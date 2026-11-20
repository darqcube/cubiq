"""mcp-prometheus's silence tools — the real app, inside the built image.

A device's alerts quiet for a while (maintenance), in Alertmanager itself: an execute client silences one device (a
matcher on its label, never a regex) for at most 7 days, with the reason and who asked; a read client lists the
silences in force and is refused making or ending one, before Alertmanager is called; bad bounds never reach it.

Needs darqcube/mcp:local built from this tree (docker compose build mcp-infrahub). No running stack;
silences_mcp_check.py does the work inside the image.
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
    "an_execute_client_silences_one_device_for_a_bounded_time", "a_read_client_is_refused_before_alertmanager_is_called",
    "the_time_and_the_reason_are_bounded", "a_read_client_lists_the_silences_in_force",
    "an_execute_client_ends_one_and_a_bad_id_never_reaches_alertmanager",
    "a_client_without_a_username_is_named_by_its_client_never_its_id",
]


@pytest.fixture(scope="module")
def results() -> dict:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"{IMAGE} not built — run: docker compose build mcp-infrahub")
    out = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{TESTS}:/t:ro", IMAGE, "python", "/t/silences_mcp_check.py"],
        capture_output=True, text=True, timeout=300,
    )
    lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
    assert lines, f"the check produced no result:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}"
    return json.loads(lines[-1])


def test_every_check_ran(results):
    assert sorted(results) == sorted(EXPECTED)


@pytest.mark.parametrize("name", EXPECTED)
def test_silence_tools(results, name):
    ok, detail = results[name]
    assert ok, detail
