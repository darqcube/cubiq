"""mcp-netmiko — what the device itself says, and (optionally) changing it.

This server does NOT open SSH sessions. It calls the automation API, which owns
the credentials, the TextFSM parsing and the error handling. One code path to
devices means a capability added there is available here for free, and a fix
never has to be made twice.

CHANGING A DEVICE (MCP_ALLOW_WRITE=true): a caller with the execute scope STAGES
a change (nothing is sent; the result has its fingerprint and which lines the
device already holds), then COMMITS it once, passing the fingerprint and an
idempotency key. Read-scope callers see changes but cannot make them. Whether a
person approved a change is the connected platform's business: this stack only
authenticates. docs/how-to/run-changes.md
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from mcp.server.fastmcp import Context

from common import Backend, build, caller, identifier, require_scope

# 300s: a config push is four sequential device commands — fetch the running
# config, snapshot state, push, snapshot again — and fetching a large config
# from a busy device is slow on its own. A client timeout shorter than the work
# leaves the change applied and the caller told it failed, which is the worst
# possible outcome for a write.
api = Backend(os.environ.get("AUTOMATION_URL", "http://automation:8100"), timeout=300.0)
mcp = build("netmiko")

ALLOW_WRITE = os.environ.get("MCP_ALLOW_WRITE", "false").lower() == "true"

CHANGE_ID = re.compile(r"^[0-9a-f]{4}-[0-9a-f]{4}$")


@mcp.tool()
def get_device_config(device: str) -> dict:
    """Fetch the running configuration of a device.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    result = api.get(f"/device/{device}/config")
    return {
        "device": device,
        "platform": result["platform"],
        "lines": result["lines"],
        "config": result["config"],
    }


@mcp.tool()
def get_device_state(device: str) -> dict:
    """Get parsed operational state from a device — interfaces and their status.

    Returns structured rows parsed from the device's CLI output, not raw text.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    result = api.get(f"/device/{device}/state")
    return {
        "device": device,
        "platform": result["platform"],
        "command": result["command"],
        "rows": result.get("rows", []),
    }


# --- changes ---------------------------------------------------------------

def _actor(who: dict) -> dict:
    return {k: who.get(k) for k in ("user", "sub", "via", "agent")}


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def _summary(change: dict) -> dict:
    """What a model or a person needs from a change — never the archived config."""
    keys = ("id", "device", "state", "lines", "rollback", "fingerprint", "already_present", "source", "requester",
            "requester_agent", "committer", "error")
    out = {k: change.get(k) for k in keys if change.get(k) not in (None, "", [])}
    for k in ("staged_at", "committed_at", "withdrawn_at", "rollback_used_at"):
        if change.get(k):
            out[k] = _iso(change[k])
    if change.get("result"):
        out["lines_sent"] = change["result"].get("lines_sent")
        out["previous_config_archived_to"] = change["result"].get("config_archived_to")
    return out


def _call(method: str, path: str, body: dict | None = None):
    """The automation API, with its refusal reason passed on as the message."""
    try:
        return api.post(path, json=body) if method == "POST" else api.get(path)
    except RuntimeError as exc:
        match = re.search(r"-> (\d{3}): (.*)$", str(exc), re.S)
        if not match:
            raise
        status, rest = int(match.group(1)), match.group(2)
        try:
            detail = json.loads(rest).get("detail", rest)
        except ValueError:
            detail = rest
        if isinstance(detail, dict):
            detail = detail.get("error") or json.dumps(detail)
        text = detail if isinstance(detail, str) else json.dumps(detail)
        raise ValueError(f"refused: {text}" if status in (403, 409) else text) from None


def _change_id(value: str) -> str:
    if not CHANGE_ID.fullmatch(value or ""):
        raise ValueError(f"invalid change id: {value!r}")
    return value


if ALLOW_WRITE:
    # Registered ONLY when MCP_ALLOW_WRITE=true. Not "registered but refuses":
    # absent from tools/list entirely, so a caller that does not know about them
    # cannot discover them. Turning automation loose on device configuration
    # should be a deliberate act, not a side effect of a container starting.
    # Registration is the first gate; the execute scope is the second.

    @mcp.tool()
    def stage_device_change(device: str, lines: list[str], ctx: Context, rollback: list[str] | None = None,
                            source: str = "") -> dict:
        """Stage a configuration change. NOTHING IS SENT TO THE DEVICE.

        The lines are checked (one command each, protected lines refused), the device's running configuration is
        read, and the change is stored with a fingerprint of both. The result says which lines the device already
        holds (the dry run). Commit it with commit_device_change, passing that fingerprint. Needs the execute scope.

        Args:
            device: the device name as it appears in the source of truth.
            lines: configuration lines to apply, in order, one command each.
            rollback: the lines that undo the change, pushed once by rollback_device_change.
            source: where the change was asked for, e.g. "chat" or "run 14" (max 64 characters).
        """
        identifier(device, "device")
        who = require_scope(ctx, "execute")
        change = _call("POST", "/changes", {"actor": _actor(who), "device": device, "lines": lines,
                                            "rollback": rollback or [], "source": source[:64]})
        out = _summary(change)
        out["next"] = ("Staged, nothing sent. Push it with commit_device_change(change_id, fingerprint, "
                       "idempotency_key); it is refused if the device's configuration changes before then.")
        return out

    @mcp.tool()
    def commit_device_change(change_id: str, fingerprint: str, idempotency_key: str, ctx: Context) -> dict:
        """Push a staged change to its device. THIS CHANGES THE NETWORK.

        Only with the fingerprint stage_device_change returned, and only once: repeating the call with the same
        idempotency key returns the first result and pushes nothing; another key is refused. Refused before anything
        is sent if the device's configuration changed since the stage. The running configuration is archived first.
        Needs the execute scope.

        Args:
            change_id: the id stage_device_change returned, e.g. 7f3a-c21e.
            fingerprint: the fingerprint stage_device_change returned.
            idempotency_key: one value per intended push (8 to 128 letters, digits or ._:-), reused on a retry.
        """
        who = require_scope(ctx, "execute")
        return _summary(_call("POST", f"/changes/{_change_id(change_id)}/commit",
                              {"actor": _actor(who), "fingerprint": fingerprint, "idempotency_key": idempotency_key}))

    @mcp.tool()
    def rollback_device_change(change_id: str, idempotency_key: str, ctx: Context) -> dict:
        """Push a committed change's declared rollback, once. THIS CHANGES THE NETWORK. Needs the execute scope.

        Args:
            change_id: the id of a committed change that declared a rollback.
            idempotency_key: one value per intended rollback, reused on a retry.
        """
        who = require_scope(ctx, "execute")
        return _summary(_call("POST", f"/changes/{_change_id(change_id)}/rollback",
                              {"actor": _actor(who), "idempotency_key": idempotency_key}))

    @mcp.tool()
    def withdraw_device_change(change_id: str, ctx: Context) -> dict:
        """Withdraw a staged change that was not committed. Needs the execute scope.

        Args:
            change_id: the id stage_device_change returned.
        """
        who = require_scope(ctx, "execute")
        return _summary(_call("POST", f"/changes/{_change_id(change_id)}/withdraw", {"actor": _actor(who)}))

    @mcp.tool()
    def get_device_change(change_id: str, ctx: Context) -> dict:
        """A change and where it stands: staged, committing, committed, withdrawn or failed.

        Args:
            change_id: the id stage_device_change returned, e.g. 7f3a-c21e.
        """
        require_scope(ctx, "read")
        return _summary(_call("GET", f"/changes/{_change_id(change_id)}"))
