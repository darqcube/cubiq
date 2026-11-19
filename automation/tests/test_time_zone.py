"""Every container follows the host's time zone, and `make logs` shows it (2026-11-19).

The lab's VM runs in Asia/Karachi and read UTC everywhere else; every compose service now mounts the host's
/etc/localtime and /etc/timezone, read-only. A service added later without them fails here, not in an operator's
logs. Prometheus, Alertmanager, Loki and Telegraf still write UTC whatever the zone (by design), so `make logs`
converts them on display. Nothing stored changes: data and APIs stay UTC.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = sorted((ROOT / "compose").glob("*.yaml"))
MOUNTS = ("/etc/localtime:/etc/localtime:ro", "/etc/timezone:/etc/timezone:ro")


def services() -> list[tuple[str, str, dict]]:
    out = []
    for f in COMPOSE:
        doc = yaml.safe_load(f.read_text())          # SafeLoader resolves the `<<: *mcp` merges
        for name, svc in (doc.get("services") or {}).items():
            out.append((f.name, name, svc))
    return out


def test_there_are_services_to_check():
    assert len(services()) >= 24, "the compose files were read"


@pytest.mark.parametrize("file,name,svc", services(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_service_mounts_the_hosts_time_zone(file, name, svc):
    volumes = [str(v).split("#")[0].strip() for v in svc.get("volumes") or []]
    for mount in MOUNTS:
        assert mount in volumes, f"{file}: {name} does not mount {mount} (it would read UTC, not the host's zone)"


def converter():
    spec = importlib.util.spec_from_file_location("local_time_logs", ROOT / "scripts/local-time-logs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_converter_shows_the_hosts_zone():
    from zoneinfo import ZoneInfo
    mod, tz = converter(), ZoneInfo("Asia/Karachi")
    assert mod.convert("time=2026-11-19T10:19:52.568Z level=INFO", tz) == "time=2026-11-19T15:19:52.568+05:00 level=INFO"
    assert mod.convert("ts=2026-11-19T10:19:53.716057663Z c=1", tz) == "ts=2026-11-19T15:19:53.716057663+05:00 c=1"
    assert mod.convert("2026-11-19T10:19:54Z I! up", tz) == "2026-11-19T15:19:54+05:00 I! up"
    assert mod.convert("ts=2026-11-19T23:30:00Z", tz) == "ts=2026-11-20T04:30:00+05:00", "the day rolls over"


def test_the_converter_leaves_the_rest_alone():
    from zoneinfo import ZoneInfo
    mod, tz = converter(), ZoneInfo("Asia/Karachi")
    for line in ("t=2026-11-19T15:19:53.474+05:00 kept", "build 2026-11-19 done", "no stamp here", "2026-13-45T99:99:99Z"):
        assert mod.convert(line, tz) == line


def test_make_logs_uses_it_unless_utc_is_asked_for():
    makefile = (ROOT / "Makefile").read_text()
    logs = makefile[makefile.index("\nlogs:"):].split("\n\n")[0]
    assert "scripts/local-time-logs.py" in logs and "UTC" in logs


def test_preflight_says_so_when_the_files_are_missing():
    """Docker makes a directory of a missing bind-mount source: the preflight names the fix first."""
    text = (ROOT / "scripts/preflight.sh").read_text()
    assert "/etc/localtime" in text and "/etc/timezone" in text and "timedatectl set-timezone" in text
