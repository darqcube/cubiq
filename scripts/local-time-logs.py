#!/usr/bin/env python3
"""Rewrites the UTC timestamps in a log stream (stdin -> stdout) into this host's time zone.

Prometheus, Alertmanager, Loki and Telegraf write UTC ("2026-11-19T10:19:52.568Z") whatever the container's zone,
by design. `make logs` pipes through this so every service reads in the zone the host (and the containers' clocks)
use. Only timestamps ending in Z are changed; one with an offset is already explicit and stays as written.
The zone comes from TZ, else /etc/timezone, else UTC (then nothing changes). Standard library only.
"""
import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def zone() -> ZoneInfo | None:
    name = os.environ.get("TZ", "").strip().lstrip(":")
    if not name:
        try:
            name = open("/etc/timezone").read().strip()
        except OSError:
            name = ""
    try:
        return ZoneInfo(name) if name and name != "UTC" else None
    except Exception:
        return None


# 2026-11-19T10:19:52Z | 2026-11-19T10:19:52.568Z | 2026-11-19 10:19:52.568123456Z (Docker's -t, nanoseconds)
STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d{1,9})?Z")


def convert(line: str, tz: ZoneInfo) -> str:
    def one(m: re.Match) -> str:
        try:
            when = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return m.group(0)
        local = when.astimezone(tz)
        return f"{local:%Y-%m-%dT%H:%M:%S}{m.group(3) or ''}{local:%z}"[:-2] + ":" + f"{local:%z}"[-2:]
    return STAMP.sub(one, line)


def main() -> None:
    tz = zone()
    for line in sys.stdin:
        sys.stdout.write(line if tz is None else convert(line, tz))
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
