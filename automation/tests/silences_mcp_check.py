"""Runs INSIDE darqcube/mcp:local (test_silences_mcp.py starts it): mcp-prometheus's silence tools, the real app.

Prints one JSON line: {check name: [passed, detail]}. Alertmanager is a recorder: what reaches it is the point.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

os.environ.update({
    "MCP_AUTH_TOKEN": "test-token",
    "MCP_EXECUTE_TOKEN": "exec-token",
    "MCP_AUTH_MODE": "both",
    "MCP_OIDC_PLATFORMS": json.dumps([{"name": "lab", "issuer": "http://idp.example/realms/demo",
                                       "jwks_url": "http://idp.example/jwks", "audiences": ["cubiq"],
                                       "clients": {"platform-control": "execute", "platform-preprod": "read"}}]),
    "PROMETHEUS_URL": "http://127.0.0.1:9",
    "ALERTMANAGER_URL": "http://127.0.0.1:9",
})
sys.path.insert(0, "/app")

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import common  # noqa: E402
from servers import prometheus  # noqa: E402

ISSUER = json.loads(os.environ["MCP_OIDC_PLATFORMS"])[0]["issuer"]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
jwt.PyJWKClient.fetch_data = lambda self: {"keys": [JWK]}

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "check", "version": "0"}}}
SILENCE = "0b6c8f8e-2b5e-4c1e-9a2f-6b7a3c1d9e01"
results: dict[str, list] = {}


def token(azp: str, username: bool = True) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "cubiq", "sub": "0efb837e-04a3-4c1e-9a2f-6b7a3c1d9e01", "azp": azp,
              "iat": now, "exp": now + 300, **({"preferred_username": f"service-account-{azp}"} if username else {})}
    return "Bearer " + jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test-key"})


class Recorder:
    """Stands in for Alertmanager: records each call, answers like it."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(self, path, params=None):
        self.calls.append(("GET", path, None))
        return [{"id": SILENCE, "status": {"state": "active"}, "startsAt": "2026-11-20T00:00:00Z",
                 "endsAt": "2026-11-20T04:00:00Z", "createdBy": "noc-manager via service-account-platform-control",
                 "comment": "Maintenance", "matchers": [{"name": "device", "value": "router1", "isRegex": False}]},
                {"id": "x", "status": {"state": "expired"}, "matchers": [{"name": "device", "value": "router1"}]},
                {"id": "y", "status": {"state": "active"}, "matchers": [{"name": "device", "value": "router2"}]}]

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return {"silenceID": SILENCE}

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        return ""


AM = Recorder()
prometheus.alertmanager = AM
APP = common._Auth(prometheus.mcp.streamable_http_app(), common.Verifier(), "prometheus")


def messages(response) -> list[dict]:
    if response.headers.get("content-type", "").startswith("application/json"):
        return [response.json()]
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def session(client, auth: str) -> dict:
    first = client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": auth, "Host": "127.0.0.1:9002"})
    assert first.status_code == 200, first.status_code
    s = {"mcp-session-id": first.headers["mcp-session-id"], "Authorization": auth, "Host": "127.0.0.1:9002", **HEADERS}
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=s)
    return s


def call(client, s, name, arguments) -> dict:
    reply = client.post("/mcp", headers=s, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                 "params": {"name": name, "arguments": arguments}})
    return messages(reply)[-1]["result"]


def text(result: dict) -> str:
    return " ".join(part.get("text", "") for part in result.get("content", []))


def check(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception:
        ok, detail = False, traceback.format_exc()[-600:]
    results[name] = [bool(ok), str(detail)[:600]]


with TestClient(APP) as c:
    execute, read = session(c, token("platform-control")), session(c, token("platform-preprod"))

    def an_execute_client_silences_one_device_for_a_bounded_time():
        AM.calls.clear()
        out = call(c, execute, "silence_device", {"device": "router1", "hours": 4, "reason": "Maintenance: fan",
                                                  "requested_by": "noc-manager"})
        method, path, body = AM.calls[-1]
        ends = datetime.fromisoformat(body["endsAt"].replace("Z", "+00:00"))
        hours = (ends - datetime.now(timezone.utc)).total_seconds() / 3600
        ok = (not out.get("isError") and method == "POST" and path == "/api/v2/silences"
              and body["matchers"] == [{"name": "device", "value": "router1", "isRegex": False, "isEqual": True}]
              and 3.9 < hours <= 4 and body["comment"] == "Maintenance: fan"
              and body["createdBy"] == "noc-manager via service-account-platform-control" and SILENCE in text(out))
        return ok, (out, AM.calls[-1])

    def a_read_client_is_refused_before_alertmanager_is_called():
        AM.calls.clear()
        one = call(c, read, "silence_device", {"device": "router1", "hours": 1, "reason": "Maintenance"})
        two = call(c, read, "end_silence", {"silence_id": SILENCE})
        return (one.get("isError") and two.get("isError") and "execute" in text(one) and not AM.calls), (one, two)

    def the_time_and_the_reason_are_bounded():
        AM.calls.clear()
        refused = [call(c, execute, "silence_device", args) for args in (
            {"device": "router1", "hours": 0, "reason": "Maintenance"},
            {"device": "router1", "hours": 169, "reason": "Maintenance"},
            {"device": "router1", "hours": 2, "reason": "x"},
            {"device": "router1; drop", "hours": 2, "reason": "Maintenance"})]
        return all(r.get("isError") for r in refused) and not AM.calls, [text(r) for r in refused]

    def a_read_client_lists_the_silences_in_force():
        AM.calls.clear()
        out = call(c, read, "list_silences", {"device": "router1"})
        body = json.loads(text(out)) if text(out).startswith("[") else out.get("structuredContent", {}).get("result")
        ids = [s["id"] for s in (body or [])]
        return (not out.get("isError") and ids == [SILENCE]), out

    def a_client_without_a_username_is_named_by_its_client_never_its_id():
        AM.calls.clear()
        bare = session(c, token("platform-control", username=False))
        call(c, bare, "silence_device", {"device": "router1", "hours": 1, "reason": "Maintenance", "requested_by": "noc-manager"})
        return AM.calls[-1][2]["createdBy"] == "noc-manager via platform-control", AM.calls[-1][2]["createdBy"]

    def an_execute_client_ends_one_and_a_bad_id_never_reaches_alertmanager():
        AM.calls.clear()
        bad = call(c, execute, "end_silence", {"silence_id": "../silences"})
        ended = call(c, execute, "end_silence", {"silence_id": SILENCE})
        return (bad.get("isError") and not ended.get("isError")
                and AM.calls == [("DELETE", f"/api/v2/silence/{SILENCE}", None)]), (bad, ended, AM.calls)

    for fn in (an_execute_client_silences_one_device_for_a_bounded_time, a_read_client_is_refused_before_alertmanager_is_called,
               the_time_and_the_reason_are_bounded, a_read_client_lists_the_silences_in_force,
               a_client_without_a_username_is_named_by_its_client_never_its_id,
               an_execute_client_ends_one_and_a_bad_id_never_reaches_alertmanager):
        check(fn.__name__, fn)

print(json.dumps(results))
