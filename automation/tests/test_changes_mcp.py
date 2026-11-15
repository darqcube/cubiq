"""mcp-netmiko's change tools — the real app, inside the built image.

Who gets through and what reaches the automation API: an execute client (or
the execute token) stages and commits; a read client, the read token and a
person's token — whatever roles it carries — are refused by every change tool;
no tool approves and the 1.x approvals path is gone; commit carries its
fingerprint and idempotency key. The rules themselves: test_changes.py.

Needs darqcube/mcp:local built from this tree (docker compose build mcp-infrahub).
No running stack; changes_mcp_check.py does the work inside the image.
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
    "tools_are_stage_commit_rollback_withdraw_and_never_approve", "the_1x_approvals_path_is_gone",
    "stage_passes_the_verified_caller_and_agent_only", "commit_carries_the_fingerprint_and_the_idempotency_key",
    "a_read_client_is_refused_by_every_change_tool", "a_persons_roles_count_for_nothing",
    "the_read_token_is_refused_by_every_change_tool", "a_read_client_reads_a_change", "the_execute_token_stages",
    "a_refusal_reaches_the_model_with_its_reason", "a_malformed_change_id_never_reaches_the_api",
    "the_audit_line_names_the_agent_and_scope",
]


@pytest.fixture(scope="module")
def results() -> dict:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"{IMAGE} not built — run: docker compose build mcp-infrahub")
    out = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{TESTS}:/t:ro", IMAGE, "python", "/t/changes_mcp_check.py"],
        capture_output=True, text=True, timeout=300,
    )
    lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
    assert lines, f"the check produced no result:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}"
    return json.loads(lines[-1])


def test_every_check_ran(results):
    assert sorted(results) == sorted(EXPECTED)


@pytest.mark.parametrize("name", EXPECTED)
def test_change_tools_gate(results, name):
    ok, detail = results[name]
    assert ok, detail
