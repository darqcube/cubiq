"""Runs INSIDE darqcube/mcp:local, driven by test_changes_mcp.py.

The change tools of mcp-netmiko, on the real ASGI app behind the stack's auth
middleware. The automation API is replaced by a recorder (the rules themselves
are tested offline in test_changes.py and in the API check), so what is pinned
here is who gets through and what reaches the API:

- the tools are stage, commit, rollback, withdraw and get: none approves, and
  the 1.x approvals path (/changes for people) is gone
- an execute client (agenticops.yml) stages and commits; a read client and a
  person's token, whatever roles it carries, are refused by every change tool
- the verified caller and agent reach the API, never values a caller sent
- commit carries the fingerprint and the idempotency key it was given
- a refusal reaches the model as a refusal with its reason

Prints one JSON object: {check name: [passed, detail]}.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback

os.environ.update({
    "MCP_ALLOW_WRITE": "true",
    "MCP_AUTH_TOKEN": "test-token",
    "MCP_EXECUTE_TOKEN": "exec-token",
    "MCP_AUTH_MODE": "both",
    "MCP_OIDC_ISSUER": "http://idp.example/realms/demo",
    "MCP_OIDC_JWKS_URL": "http://idp.example/jwks",
    "MCP_OIDC_AUDIENCES": "cubiq",
    "MCP_OIDC_CLIENT_SCOPES": "platform-control=execute,platform-preprod=read",
    "AUTOMATION_URL": "http://127.0.0.1:9",
})
sys.path.insert(0, "/app")

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import common  # noqa: E402
from servers import netmiko  # noqa: E402

ISSUER = os.environ["MCP_OIDC_ISSUER"]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
jwt.PyJWKClient.fetch_data = lambda self: {"keys": [JWK]}

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "check", "version": "0"}}}
results: dict[str, list] = {}


def token(azp="platform-control", sub="u-1", user="alice",
          roles=("darqcube-propose", "darqcube-approve", "darqcube-commit")) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "cubiq", "sub": sub, "preferred_username": user, "azp": azp,
              "iat": now, "exp": now + 300, "realm_access": {"roles": list(roles)}}
    return "Bearer " + jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test-key"})


class Recorder:
    """Stands in for the automation API: records each call, answers like it."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.refuse: str | None = None

    def _change(self, actor, state="staged"):
        return {"id": "7f3a-c21e", "device": "router1", "state": state, "lines": ["x"], "rollback": [],
                "fingerprint": "f" * 64, "already_present": [], "requester": actor.get("user") if actor else "bob",
                "requester_sub": actor.get("sub") if actor else "u-2", "staged_at": time.time()}

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        if self.refuse:
            raise RuntimeError(f"POST {path} -> 409: " + __import__("json").dumps({"detail": self.refuse}))
        return self._change((json or {}).get("actor"), "committed" if path.endswith("/commit") else "staged")

    def get(self, path, params=None):
        self.calls.append(("GET", path, None))
        return self._change(None)


API = Recorder()
netmiko.api = API

APP = common._Auth(netmiko.mcp.streamable_http_app(), common.Verifier(), "netmiko")


def messages(response) -> list[dict]:
    if response.headers.get("content-type", "").startswith("application/json"):
        return [response.json()]
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def session(client, auth: str) -> dict:
    first = client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": auth, "Host": "127.0.0.1:9005"})
    if first.status_code != 200:
        return {"status": first.status_code}
    s = {"mcp-session-id": first.headers["mcp-session-id"], "Authorization": auth, "Host": "127.0.0.1:9005", **HEADERS}
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=s)
    return s


def rpc(client, s: dict, method: str, params: dict) -> dict:
    reply = client.post("/mcp", headers=s, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
    return messages(reply)[-1]


def call(client, s, name, arguments) -> dict:
    return rpc(client, s, "tools/call", {"name": name, "arguments": arguments})["result"]


def check(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception:
        ok, detail = False, traceback.format_exc()[-600:]
    results[name] = [bool(ok), str(detail)[:600]]


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


capture = Capture()
logging.getLogger("mcp.audit").addHandler(capture)
logging.getLogger("mcp.audit").setLevel(logging.INFO)

with TestClient(APP) as c:
    control = session(c, token())

    def tools_list():
        names = sorted(t["name"] for t in rpc(c, control, "tools/list", {})["result"]["tools"])
        want = ["commit_device_change", "get_device_change", "get_device_config", "get_device_state",
                "rollback_device_change", "stage_device_change", "withdraw_device_change"]
        return names == want and not [n for n in names if "approve" in n or "push" in n], names
    check("tools_are_stage_commit_rollback_withdraw_and_never_approve", tools_list)

    def approvals_path_gone():
        r = c.get("/changes", headers={"Authorization": token()})
        p = c.post("/changes/7f3a-c21e/approve", headers={"Authorization": token()})
        return r.status_code == p.status_code == 404 and not getattr(netmiko, "PUBLIC_ROUTES", []), \
            (r.status_code, p.status_code)
    check("the_1x_approvals_path_is_gone", approvals_path_gone)

    def stage_identity():
        API.calls.clear()
        result = call(c, control, "stage_device_change", {"device": "router1", "lines": ["interface Ethernet0/1"],
                                                          "source": "run 14"})
        method, path, body = API.calls[-1]
        actor = body["actor"]
        ok = (path == "/changes" and actor == {"user": "alice", "sub": "u-1", "via": "oidc", "agent": "platform-control"}
              and not result.get("isError"))
        return ok, actor
    check("stage_passes_the_verified_caller_and_agent_only", stage_identity)

    def commit_carries_key_and_fingerprint():
        API.calls.clear()
        result = call(c, control, "commit_device_change",
                      {"change_id": "7f3a-c21e", "fingerprint": "f" * 64, "idempotency_key": "run14-commit"})
        method, path, body = API.calls[-1]
        ok = (path == "/changes/7f3a-c21e/commit" and body["fingerprint"] == "f" * 64
              and body["idempotency_key"] == "run14-commit" and not result.get("isError"))
        return ok, body
    check("commit_carries_the_fingerprint_and_the_idempotency_key", commit_carries_key_and_fingerprint)

    def refused_for(auth):
        s = session(c, auth)
        before = len(API.calls)
        out = [call(c, s, name, args) for name, args in (
            ("stage_device_change", {"device": "router1", "lines": ["x"]}),
            ("commit_device_change", {"change_id": "7f3a-c21e", "fingerprint": "f" * 64, "idempotency_key": "k-000001"}),
            ("rollback_device_change", {"change_id": "7f3a-c21e", "idempotency_key": "k-000001"}),
            ("withdraw_device_change", {"change_id": "7f3a-c21e"}))]
        return all(r.get("isError") and "needs execute" in json.dumps(r) for r in out) and len(API.calls) == before, \
            json.dumps(out)[:300]

    check("a_read_client_is_refused_by_every_change_tool", lambda: refused_for(token(azp="platform-preprod")))
    check("a_persons_roles_count_for_nothing", lambda: refused_for(token(azp="portal")))
    check("the_read_token_is_refused_by_every_change_tool", lambda: refused_for("Bearer test-token"))

    def read_client_reads():
        s = session(c, token(azp="platform-preprod"))
        result = call(c, s, "get_device_change", {"change_id": "7f3a-c21e"})
        return not result.get("isError"), json.dumps(result)[:200]
    check("a_read_client_reads_a_change", read_client_reads)

    def execute_token_stages():
        s = session(c, "Bearer exec-token")
        API.calls.clear()
        result = call(c, s, "stage_device_change", {"device": "router1", "lines": ["x"]})
        return not result.get("isError") and API.calls[-1][2]["actor"]["user"] == "execute-token", \
            json.dumps(result)[:200]
    check("the_execute_token_stages", execute_token_stages)

    def refusal_reaches_the_model():
        API.refuse = "the fingerprint is not this change's: commit exactly what was staged"
        try:
            result = call(c, control, "commit_device_change",
                          {"change_id": "7f3a-c21e", "fingerprint": "0" * 64, "idempotency_key": "k-000002"})
        finally:
            API.refuse = None
        text = json.dumps(result)
        return result.get("isError") and "refused: the fingerprint is not this change's" in text, text[:300]
    check("a_refusal_reaches_the_model_with_its_reason", refusal_reaches_the_model)

    def bad_id():
        before = len(API.calls)
        result = call(c, control, "commit_device_change",
                      {"change_id": "../../device/router1/config", "fingerprint": "f", "idempotency_key": "k-000003"})
        return result.get("isError") and len(API.calls) == before, json.dumps(result)[:200]
    check("a_malformed_change_id_never_reaches_the_api", bad_id)

    def audit_names_agent():
        line = next((m for m in capture.records if "tool=stage_device_change" in m), "")
        return "agent=platform-control" in line and "user=alice" in line and "scope=execute" in line, line
    check("the_audit_line_names_the_agent_and_scope", audit_names_agent)

print(json.dumps(results))
