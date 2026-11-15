"""MCP transport and auth — the real app, inside the built image.

Pins the properties an ai-platform on another machine depends on:

- the Host allow-list: compose names, loopback and the published address are
  answered; anything else gets 421. (mcp 1.30 turned a loopback-only list on
  silently; every call through the compose network failed while /healthz
  stayed green — this is the test that would have caught it.)
- MCP_AUTH_MODE token / oidc / both: who gets in, and who gets 401.
- the scopes: read or execute, from the token used (two tokens) or the OIDC
  client (agenticops.yml's clients); a person's roles count for nothing.
- startup refuses an empty token, one token for both scopes, an unknown scope
  or a half-configured OIDC.
- tools run off the event loop.

Needs darqcube/mcp:local (docker compose build mcp-infrahub). No running stack;
mcp_transport_check.py does the work inside the image.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
IMAGE = "darqcube/mcp:local"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")

EXPECTED = [
    "host_compose_name_accepted", "host_published_address_accepted", "host_loopback_accepted", "host_unknown_refused",
    "token_missing_401", "token_wrong_401", "token_right_200", "token_jwt_refused_in_token_mode",
    "healthz_needs_no_auth", "read_token_is_refused_by_an_execute_tool", "execute_token_passes_the_scope_gate",
    "oidc_valid_200", "oidc_shared_token_refused", "oidc_wrong_issuer_401", "oidc_wrong_audience_401",
    "oidc_expired_401", "oidc_roles_count_for_nothing_only_the_client_scope",
    "oidc_read_client_is_refused_by_an_execute_tool", "oidc_execute_client_passes_the_scope_gate",
    "default_scope_none_refuses_an_unnamed_client", "default_scope_none_still_admits_a_named_client",
    "both_accepts_token", "both_accepts_jwt", "both_refuses_garbage",
    "empty_token_refused_at_startup", "oidc_without_issuer_refused_at_startup", "unknown_mode_refused_at_startup",
    "same_read_and_execute_token_refused_at_startup", "an_unknown_client_scope_is_refused_at_startup",
    "audit_names_user_scope_and_tool", "audit_omits_arguments",
    "tools_run_off_the_event_loop", "context_is_not_exposed_as_an_argument",
]



@pytest.fixture(scope="module")
def results() -> dict:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"{IMAGE} not built — run: docker compose build mcp-infrahub")
    out = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{TESTS}:/t:ro", IMAGE, "python", "/t/mcp_transport_check.py"],
        capture_output=True, text=True, timeout=300,
    )
    lines = [l for l in out.stdout.splitlines() if l.startswith("{")]
    assert lines, f"the check produced no result:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}"
    return json.loads(lines[-1])


@pytest.mark.parametrize("name", EXPECTED)
def test_transport_and_auth(results, name):
    assert name in results, f"{name} did not run — a check group failed early: {results}"
    passed, detail = results[name]
    assert passed, f"{name}: {detail}"
