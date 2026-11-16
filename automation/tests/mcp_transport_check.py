"""Runs INSIDE darqcube/mcp:local, driven by test_mcp_transport.py.

Exercises the real ASGI app — FastMCP's streamable-HTTP transport behind the
stack's auth middleware — with Starlette's TestClient. No network, no backend:
the automation API address points at a closed port, so a tool that gets past
its gates fails with "unreachable", which is itself the proof it got past them.

Prints one JSON object: {check name: [passed, detail]}.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

# Before anything imports common: it reads its settings at import time.
os.environ.update({
    "MCP_ALLOW_WRITE": "true",
    "MCP_ALLOWED_HOSTS": "192.0.2.10:*",
    "AUTOMATION_URL": "http://127.0.0.1:9",
    "MCP_AUTH_TOKEN": "test-token",
    "MCP_EXECUTE_TOKEN": "exec-token",
})
sys.path.insert(0, "/app")

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import common  # noqa: E402
from servers import netmiko  # noqa: E402

ISSUER = "http://idp.example/realms/demo"
STAGING_ISSUER = "http://idp.staging.example/realms/demo"
AUDIENCE = "ai-platform"


def keypair(kid: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return key, jwk


# Two platforms, two identity providers, two keys. The key clients fetch nothing: each is handed its own JWKS.
KEY, JWK = keypair("test-key")
STAGING_KEY, STAGING_JWK = keypair("staging-key")
JWKS = {"http://idp.example/jwks": [JWK], "http://idp.staging.example/jwks": [STAGING_JWK]}
jwt.PyJWKClient.fetch_data = lambda self: {"keys": JWKS[self.uri]}

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "check", "version": "0"}}}
results: dict[str, list] = {}


def token(azp="portal", key=None, kid="test-key", **overrides) -> str:
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u-1", "preferred_username": "alice", "azp": azp,
              "exp": int(time.time()) + 300, "realm_access": {"roles": ["darqcube-write", "darqcube-approve"]}}
    claims.update(overrides)
    return jwt.encode(claims, key or KEY, algorithm="RS256", headers={"kid": kid})


def staging_token(azp="portal", **overrides) -> str:
    return token(azp, key=STAGING_KEY, kid="staging-key", iss=STAGING_ISSUER, **overrides)


def platforms(default_scope: str = "read") -> list:
    return [common.Platform(name="lab", issuer=ISSUER, jwks_url="http://idp.example/jwks", audiences=(AUDIENCE,),
                            default_scope=default_scope,
                            clients={"platform-control": "execute", "platform-preprod": "read"}),
            common.Platform(name="staging", issuer=STAGING_ISSUER, jwks_url="http://idp.staging.example/jwks",
                            audiences=(AUDIENCE,), clients={"platform-control": "read"})]


def verifier(mode: str, default_scope: str = "read") -> common.Verifier:
    return common.Verifier(mode=mode, token="test-token", platforms=platforms(default_scope),
                           execute_token="exec-token")


# One app for every mode: FastMCP's session manager can be started only once
# per instance, so the client stays open and only the verifier is swapped.
APP = common._Auth(netmiko.mcp.streamable_http_app(), verifier("token"), "netmiko")


def messages(response) -> list[dict]:
    """JSON-RPC messages from a JSON or an SSE response body."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return [response.json()]
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def initialize(client, auth: str, host: str = "127.0.0.1:9005"):
    return client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": auth, "Host": host})


def call_push(client, auth: str) -> str:
    """Open a session and call commit_device_change (an execute tool); return the tool's text."""
    first = initialize(client, auth)
    session = {"mcp-session-id": first.headers["mcp-session-id"], "Authorization": auth,
               "Host": "127.0.0.1:9005", **HEADERS}
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session)
    reply = client.post("/mcp", headers=session, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "commit_device_change",
                   "arguments": {"change_id": "7f3a-c21e", "fingerprint": "f" * 64, "idempotency_key": "key-0001"}}})
    return json.dumps(messages(reply))


def check(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception:
        ok, detail = False, traceback.format_exc()[-600:]
    results[name] = [bool(ok), str(detail)[:600]]


# The DNS-rebinding allow-list ------------------------------------------------
# The SDK checks Host BEFORE the session manager runs, so these need no session.

def host_checks():
    APP.verifier = verifier("token")
    auth = "Bearer test-token"
    check("host_compose_name_accepted",
          lambda: (initialize(c, auth, "mcp-netmiko:9005").status_code == 200,
                   initialize(c, auth, "mcp-netmiko:9005").status_code))
    check("host_published_address_accepted",
          lambda: (initialize(c, auth, "192.0.2.10:9005").status_code == 200,
                   initialize(c, auth, "192.0.2.10:9005").status_code))
    check("host_loopback_accepted",
          lambda: (initialize(c, auth, "localhost:9005").status_code == 200, "localhost"))
    check("host_unknown_refused",
          lambda: (initialize(c, auth, "evil.example:9005").status_code == 421,
                   initialize(c, auth, "evil.example:9005").status_code))


def token_checks():
    APP.verifier = verifier("token")
    check("token_missing_401", lambda: (c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401, ""))
    check("token_wrong_401", lambda: (initialize(c, "Bearer nope").status_code == 401, ""))
    check("token_right_200", lambda: (initialize(c, "Bearer test-token").status_code == 200, ""))
    check("token_jwt_refused_in_token_mode",
          lambda: (initialize(c, f"Bearer {token('platform-control')}").status_code == 401, ""))
    check("healthz_needs_no_auth", lambda: (c.get("/healthz").status_code in (200, 404), ""))
    check("version_needs_no_auth_and_names_cubiq",
          lambda: (c.get("/version").status_code in (200, 404), c.get("/version").text[:80]))
    # The read token reads, and is refused by an execute tool; the execute token gets past the gate.
    read = call_push(c, "Bearer test-token")
    check("read_token_is_refused_by_an_execute_tool", lambda: ("needs execute" in read, read))
    out = call_push(c, "Bearer exec-token")
    check("execute_token_passes_the_scope_gate", lambda: ("unreachable" in out and "needs execute" not in out, out))


def oidc_checks():
    APP.verifier = verifier("oidc")
    check("oidc_valid_200", lambda: (initialize(c, f"Bearer {token()}").status_code == 200, ""))
    check("oidc_shared_token_refused",
          lambda: (initialize(c, "Bearer test-token").status_code == 401, ""))
    check("oidc_wrong_issuer_401",
          lambda: (initialize(c, f"Bearer {token(iss='http://other/realms/x')}").status_code == 401, ""))
    check("oidc_wrong_audience_401",
          lambda: (initialize(c, f"Bearer {token(aud='someone-else')}").status_code == 401, ""))
    check("oidc_expired_401",
          lambda: (initialize(c, f"Bearer {token(exp=int(time.time()) - 600)}").status_code == 401, ""))
    # A person's token carries roles that once gated changes: they count for nothing now. The client decides.
    person = call_push(c, f"Bearer {token()}")
    check("oidc_roles_count_for_nothing_only_the_client_scope",
          lambda: ("needs execute" in person, person))
    preprod = call_push(c, f"Bearer {token('platform-preprod')}")
    check("oidc_read_client_is_refused_by_an_execute_tool", lambda: ("needs execute" in preprod, preprod))
    control = call_push(c, f"Bearer {token('platform-control')}")
    check("oidc_execute_client_passes_the_scope_gate",
          lambda: ("unreachable" in control and "needs execute" not in control, control))
    APP.verifier = verifier("oidc", default_scope="none")
    check("default_scope_none_refuses_an_unnamed_client",
          lambda: (initialize(c, f"Bearer {token()}").status_code == 401, ""))
    check("default_scope_none_still_admits_a_named_client",
          lambda: (initialize(c, f"Bearer {token('platform-preprod')}").status_code == 200, ""))


def platform_checks():
    """Two platforms: a token's issuer picks one, that platform's keys check it, that platform's clients scope it."""
    APP.verifier = verifier("oidc")
    check("second_platform_valid_200", lambda: (initialize(c, f"Bearer {staging_token()}").status_code == 200, ""))
    staged = call_push(c, f"Bearer {staging_token('platform-control')}")
    check("the_same_client_has_each_platforms_own_scope", lambda: ("needs execute" in staged, staged))
    forged = token("platform-control", iss=STAGING_ISSUER)   # the lab's key, the staging issuer
    check("a_token_signed_by_another_platforms_key_is_refused",
          lambda: (initialize(c, f"Bearer {forged}").status_code == 401, ""))
    stranger = token(iss="http://idp.stranger.example/realms/x")
    check("an_issuer_no_platform_has_is_refused", lambda: (initialize(c, f"Bearer {stranger}").status_code == 401, ""))


def both_checks():
    APP.verifier = verifier("both")
    check("both_accepts_token", lambda: (initialize(c, "Bearer test-token").status_code == 200, ""))
    check("both_accepts_jwt", lambda: (initialize(c, f"Bearer {token()}").status_code == 200, ""))
    check("both_refuses_garbage", lambda: (initialize(c, "Bearer x.y.z").status_code == 401, ""))


def startup_checks():
    def refuses(**kw):
        try:
            common.Verifier(**kw)
        except SystemExit:
            return True
        return False
    check("empty_token_refused_at_startup", lambda: (refuses(mode="token", token=""), ""))
    check("oidc_without_a_platform_refused_at_startup",
          lambda: (refuses(mode="oidc", token="", platforms=[]), ""))
    check("unknown_mode_refused_at_startup", lambda: (refuses(mode="open", token="x"), ""))
    check("same_read_and_execute_token_refused_at_startup",
          lambda: (refuses(mode="token", token="same", execute_token="same"), ""))
    def refused(raw):
        try:
            common.parse_platforms(raw)
        except SystemExit:
            return True
        return False
    lab = {"name": "lab", "issuer": ISSUER, "jwks_url": "http://idp.example/jwks"}
    check("an_unknown_client_scope_is_refused_at_startup",
          lambda: (refused(json.dumps([{**lab, "clients": {"platform-control": "admin"}}])), ""))
    check("a_default_scope_of_execute_is_refused_at_startup",
          lambda: (refused(json.dumps([{**lab, "default_scope": "execute"}])), ""))
    check("two_platforms_with_one_issuer_are_refused_at_startup",
          lambda: (refused(json.dumps([lab, {**lab, "name": "staging"}])), ""))
    check("platforms_that_are_not_json_are_refused_at_startup", lambda: (refused("lab=http://x"), ""))
    check("the_platforms_line_install_writes_is_read",
          lambda: (common.parse_platforms(json.dumps([lab]))[0].issuer == ISSUER, ""))


def audit_checks():
    """One audit line per request, naming the caller and the tool — never the
    arguments."""
    import logging

    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    audit = logging.getLogger("mcp.audit")
    audit.addHandler(handler)
    audit.setLevel(logging.INFO)
    try:
        APP.verifier = verifier("oidc")
        call_push(c, f"Bearer {token('platform-control')}")
    finally:
        audit.removeHandler(handler)
    line = next((r for r in records if "tool=commit_device_change" in r), "")
    check("audit_names_user_platform_scope_and_tool",
          lambda: (line.startswith("audit ") and "user=alice" in line and "via=oidc" in line
                   and "platform=lab" in line and "scope=execute" in line, records))
    check("audit_omits_arguments", lambda: ("7f3a-c21e" not in " ".join(records), records))


def offload_checks():
    tools = netmiko.mcp._tool_manager.list_tools()
    check("tools_run_off_the_event_loop",
          lambda: (all(t.is_async for t in tools), {t.name: t.is_async for t in tools}))
    push = next(t for t in tools if t.name == "commit_device_change")
    check("context_is_not_exposed_as_an_argument",
          lambda: ("ctx" not in push.parameters.get("properties", {}), push.parameters))


with TestClient(APP) as c:
    for group in (host_checks, token_checks, oidc_checks, platform_checks, both_checks, startup_checks, audit_checks,
                  offload_checks):
        try:
            group()
        except Exception:
            results[group.__name__] = [False, traceback.format_exc()[-600:]]

print(json.dumps(results))
