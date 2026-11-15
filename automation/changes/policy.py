"""The rules every device change follows. Pure functions: no I/O, no clock reads.

These are properties of executing a change, not policy about who may change
what. Who may call is decided by the MCP layer (the execute scope: a named
client of the identity provider, or the execute token); whether a person
approved the change is the connected platform's business, never this stack's.

  stage      the exact lines (one command each, no control characters, at most
             MAX_CONFIG_LINES), checked against protected lines, fingerprinted
             together with the device's configuration as it was then
  commit     only a staged change, only with its fingerprint, only once: a
             retry with the same idempotency key gets the first result, never a
             second push; a device whose configuration changed since staging is
             refused before anything is sent
  rollback   only a committed change that declared one, only once
  withdraw   only a staged change
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

STAGED, COMMITTING, COMMITTED, WITHDRAWN, FAILED = "staged", "committing", "committed", "withdrawn", "failed"
STATES = (STAGED, COMMITTING, COMMITTED, WITHDRAWN, FAILED)
CLOSED = (COMMITTED, WITHDRAWN, FAILED)
# 1.x states. A change left in one of them when the stack moves to 2.0.0 is closed at startup (store.py): its approval
# belonged to the 1.x rules, so it is never committed under the 2.0.0 ones.
LEGACY_OPEN = ("waiting", "approved")

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class Refused(Exception):
    """The action breaks a rule. The message says which, for a person to read."""


class Conflict(Refused):
    """The change or the device is not in the state the caller expected (HTTP 409)."""


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    max_lines: int = 200

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(max_lines=_int("MAX_CONFIG_LINES", 200))


# Lines no device-configuration comparison should count: blank lines and comments (IOS "!", VRP and RouterOS "#",
# which carry timestamps such as "! Last configuration change at …"), and IOS's self-adjusting "ntp clock-period".
_VOLATILE = re.compile(r"^\s*(?:$|!|#|ntp clock-period\b)")


def config_digest(config: str) -> str:
    """SHA-256 of a running configuration without its volatile lines: equal when nobody changed the device."""
    kept = [line.rstrip() for line in config.splitlines() if not _VOLATILE.match(line)]
    return hashlib.sha256("\n".join(kept).encode()).hexdigest()


def fingerprint(device: str, lines: list[str], rollback: list[str], digest: str = "") -> str:
    """SHA-256 of exactly what was staged: the device, the lines in order, the rollback, the device's configuration."""
    canonical = json.dumps({"device": device, "lines": lines, "rollback": rollback, "config": digest},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def already_present(config: str, lines: list[str]) -> list[str]:
    """The staged lines the running configuration already holds, word for word: the dry run's plain answer."""
    present = {line.strip() for line in config.splitlines()}
    return [line for line in lines if line.strip() in present]


def check_stage(device: str, lines: list[str], rollback: list[str], settings: Settings) -> None:
    if not lines:
        raise Refused("no configuration lines")
    if len(lines) > settings.max_lines or len(rollback) > settings.max_lines:
        raise Refused(f"more than {settings.max_lines} lines (MAX_CONFIG_LINES)")
    for line in [*lines, *rollback]:
        # One element is one command. A newline inside it would carry a second,
        # unchecked command past the protected-line rules.
        if len(line) > 512 or any(ord(ch) < 32 and ch != "\t" for ch in line):
            raise Refused(f"line {line[:40]!r}…: one command per line, no control characters, at most 512 characters")


def check_key(key: str) -> None:
    if not IDEMPOTENCY_KEY.fullmatch(key or ""):
        raise Refused("an idempotency key is required: 8 to 128 letters, digits or ._:- (one per intended push)")


def replay(change: dict, key: str, rollback: bool = False) -> bool:
    """True when this call repeats one already made with the same key: answer with the first result, push nothing."""
    used = change.get("rollback_key" if rollback else "commit_key")
    if used and used == key:
        return True
    if used:
        what = "rolled back" if rollback else "committed"
        raise Conflict(f"change {change['id']} was already {what} (with another idempotency key)")
    return False


def check_commit(change: dict, fingerprint_given: str) -> None:
    if change["state"] != STAGED:
        raise Conflict(f"change {change['id']} is {change['state']}, not {STAGED}")
    if fingerprint_given != change["fingerprint"]:
        raise Conflict("the fingerprint is not this change's: commit exactly what was staged")


def check_rollback(change: dict) -> None:
    if change["state"] != COMMITTED:
        raise Conflict(f"change {change['id']} is {change['state']}: only a pushed change rolls back")
    if not change["rollback"]:
        raise Refused(f"change {change['id']} declared no rollback")


def check_withdraw(change: dict) -> None:
    if change["state"] != STAGED:
        raise Conflict(f"change {change['id']} is {change['state']}: only a staged change is withdrawn")
