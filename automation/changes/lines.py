"""Protected lines: refused when a change is staged, before anything reaches a device.

The rules are data (protected.yml). A refusal names the line, the rule and
why, so the person who asked can see exactly what to change.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_FILE = Path(__file__).with_name("protected.yml")


class LineRuleError(ValueError):
    """protected.yml is malformed. Raised at load, never silently ignored."""


@lru_cache(maxsize=4)
def _load(path: str) -> list[tuple[str, str, re.Pattern]]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    rules = []
    for rule in data.get("rules") or []:
        try:
            rules.append((rule["name"], rule.get("why", ""), re.compile(rule["match"], re.IGNORECASE)))
        except (KeyError, TypeError, re.error) as exc:
            raise LineRuleError(f"{path}: bad rule {rule!r}: {exc}") from exc
    if data.get("management"):
        raise LineRuleError(f"{path}: 'management' moved to the connected platform in 2.0.0 (which interfaces carry "
                            "management access is its policy): remove the section")
    return rules


def refusals(lines: list[str], path: str | None = None) -> list[str]:
    """Every protected line in `lines`, as one sentence each. Empty = allowed."""
    rules = _load(path or os.environ.get("CHANGE_PROTECTED_FILE", "").strip() or str(DEFAULT_FILE))
    found = []
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        for name, why, pattern in rules:
            if pattern.search(line):
                found.append(f"line {number} “{line}”: {name} ({why})")
    return found
