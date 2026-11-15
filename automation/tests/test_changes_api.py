"""The automation API's /changes endpoints — the real app, inside the image.

Stage, then commit once, with the real store and rules: the fingerprint, the
idempotency key, a device that changed since the stage, protected lines, a
failed push recorded and never retried, every action in the execution log. Only
the device (its configuration and the push) is replaced.

Needs darqcube/automation:local (docker compose build automation). No stack,
no devices: changes_api_check.py mounts this repo's automation/ tree.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "darqcube/automation:local"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")

EXPECTED = [
    "the_raw_push_is_gone", "stage_pushes_nothing_and_commit_pushes_exactly_the_staged_lines",
    "a_retry_with_the_same_key_pushes_nothing_and_another_key_is_409", "a_wrong_fingerprint_is_409_and_pushes_nothing",
    "a_device_changed_since_the_stage_is_409_and_nothing_is_sent", "a_new_timestamp_comment_is_not_a_device_change",
    "a_protected_line_is_403_before_the_device_is_read", "an_unknown_device_is_404",
    "a_failed_push_is_recorded_and_not_retried", "a_rollback_pushes_once", "every_action_done_or_refused_is_an_event",
    "changes_list_by_state",
]


@pytest.fixture(scope="module")
def results() -> dict:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"{IMAGE} not built — run: docker compose build automation")
    out = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{ROOT / 'automation'}:/app/automation:ro",
         "-v", f"{ROOT / 'platforms.yml'}:/app/platforms.yml:ro",
         IMAGE, "python", "/app/automation/tests/changes_api_check.py"],
        capture_output=True, text=True, timeout=300,
    )
    lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
    assert lines, f"the check produced no result:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}"
    return json.loads(lines[-1])


def test_every_check_ran(results):
    assert sorted(results) == sorted(EXPECTED)


@pytest.mark.parametrize("name", EXPECTED)
def test_changes_api(results, name):
    ok, detail = results[name]
    assert ok, detail
