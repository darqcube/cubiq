"""mcp-prometheus — what IS happening, as metrics."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import Context

from common import Backend, BoundsError, build, duration, identifier, limit, require_scope

api = Backend(os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"))
alertmanager = Backend(os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093"))
mcp = build("prometheus")

SILENCE_MAX_HOURS = 7 * 24
SILENCE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# No raw-PromQL tool. Each tool below builds a bounded query from validated
# arguments, so the blast radius of a bad call is a bad answer, not a dead TSDB.


def _query(promql: str) -> list[dict]:
    body = api.get("/api/v1/query", params={"query": promql})
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus: {body.get('error', 'query failed')}")
    return body["data"]["result"]


def _rows(result: list[dict], value_name: str = "value") -> list[dict]:
    return [
        {**r["metric"], value_name: float(r["value"][1])}
        for r in result
        if "value" in r
    ]


@mcp.tool()
def get_device_metrics(device: str) -> dict:
    """CPU, memory and uptime for one device.

    Args:
        device: the device name as labelled in the source of truth.
    """
    identifier(device, "device")
    sel = f'{{device="{device}"}}'
    return {
        "device": device,
        "cpu_percent": _rows(_query(f"device:cpu_usage:max{sel}")),
        "memory_percent": _rows(_query(f"device:memory_used_percent:max{sel}")),
        "uptime_seconds": _rows(_query(f"device_uptime{sel}")),
    }


@mcp.tool()
def get_interface_status(device: str) -> dict:
    """Every interface on a device, with admin and operational state.

    Status values follow IF-MIB: 1 = up, 2 = down.

    Args:
        device: the device name.
    """
    identifier(device, "device")
    oper = _rows(_query(f'interface_oper_status{{device="{device}"}}'), "oper")
    admin = {
        (r["ifName"]): r["admin"]
        for r in _rows(_query(f'interface_admin_status{{device="{device}"}}'), "admin")
    }
    interfaces = [
        {
            "name": r.get("ifName"),
            "description": r.get("ifAlias", ""),
            "type": r.get("ifType"),
            "oper_up": r["oper"] == 1,
            "admin_up": admin.get(r.get("ifName")) == 1,
        }
        for r in oper
    ]
    return {
        "device": device,
        "count": len(interfaces),
        "down": [i for i in interfaces if i["admin_up"] and not i["oper_up"]],
        "interfaces": interfaces,
    }


@mcp.tool()
def list_alerts(state: str = "firing") -> dict:
    """Currently firing (or pending) alerts across the network.

    Args:
        state: "firing" or "pending".
    """
    if state not in ("firing", "pending"):
        raise ValueError('state must be "firing" or "pending"')
    rows = _rows(_query(f'ALERTS{{alertstate="{state}"}}'))
    return {
        "state": state,
        "count": len(rows),
        "alerts": [
            {
                "alert": r.get("alertname"),
                "device": r.get("device"),
                "severity": r.get("severity"),
                "interface": r.get("ifName"),
            }
            for r in rows
        ],
    }


@mcp.tool()
def get_flow_summary(device: str | None = None, window: str = "1h") -> dict:
    """Traffic volume from NetFlow/IPFIX, by protocol and by application.

    Applications come from the services modelled in the source of truth;
    unmodelled traffic is application "other".

    Per-flow addresses and ports are deliberately not collected, so this
    answers "how much traffic, of what kind" but NOT "which host is the top
    talker" — that needs a flow store this stack does not run.

    Args:
        device: limit to one device, or omit for the whole network.
        window: time window, e.g. "15m", "6h". Maximum 24h.
    """
    window = duration(window)
    sel = f'{{device="{identifier(device, "device")}"}}' if device else ""
    # netflow_flow_bytes_total is a gauge: bytes per 60 s window, stored once
    # per window (Telegraf exports the window's timestamp); a minute without
    # flows has no sample. Total over the window = the sum of its windows.
    # (A mean of the samples x minutes counted a lone burst, such as an OSPF
    # refresh every 30 minutes, as if it lasted the whole window.)
    volume = f"sum_over_time(netflow_flow_bytes_total{sel}[{window}])"
    by_protocol = _rows(_query(f"sum by (device, protocol) ({volume})"), "bytes")
    by_application = _rows(_query(f"sum by (application, criticality) ({volume})"), "bytes")
    return {
        "window": window,
        "device": device or "all",
        "by_protocol": sorted(by_protocol, key=lambda r: -r["bytes"]),
        "by_application": sorted(by_application, key=lambda r: -r["bytes"]),
    }


@mcp.tool()
def get_down_interfaces(max_results: int | None = None) -> dict:
    """Every interface across the network that is admin-up but operationally down.

    Args:
        max_results: cap on returned rows (default 100, maximum 1000).
    """
    n = limit(max_results)
    rows = _rows(_query("interface_oper_status == 2 and interface_admin_status == 1"))
    return {
        "count": len(rows),
        "interfaces": [
            {
                "device": r.get("device"),
                "site": r.get("site"),
                "interface": r.get("ifName"),
                "description": r.get("ifAlias", ""),
            }
            for r in rows[:n]
        ],
    }


# --- Silences: a device's alerts quiet for a while (maintenance) ----------------------------------------------------
# Alertmanager's own silences, so every receiver stays quiet, not one. Only by device (a matcher on its label, never a
# regex) and only for a bounded time: a silence that outlives its reason hides the next real fault. Reading them needs
# the read scope; making or ending one, the execute scope (a connected platform decides who may ask, Cubiq only that
# the caller holds execute).


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@mcp.tool()
def list_silences(device: str = "") -> list[dict]:
    """The silences in force now (Alertmanager), optionally only one device's.

    Args:
        device: a device name to narrow to, or empty for every silence in force.
    """
    if device:
        identifier(device, "device")
    out = []
    for s in alertmanager.get("/api/v2/silences") or []:
        if (s.get("status") or {}).get("state") != "active":
            continue
        devices = [m.get("value") for m in s.get("matchers") or [] if m.get("name") == "device" and not m.get("isRegex")]
        if device and device not in devices:
            continue
        out.append({"id": s.get("id"), "devices": devices, "starts_at": s.get("startsAt"), "ends_at": s.get("endsAt"),
                    "created_by": s.get("createdBy"), "comment": s.get("comment")})
    return out


@mcp.tool()
def silence_device(device: str, hours: int, reason: str, ctx: Context, requested_by: str = "") -> dict:
    """Silence every alert of one device for a while (maintenance): no receiver is notified until it ends.

    Needs the execute scope. At most 7 days; end it early with end_silence.

    Args:
        device: the device name, as its alerts label it.
        hours: how long, 1 to 168.
        reason: why, in a few words (shown with the silence).
        requested_by: the person the caller acts for, if any (kept with the silence).
    """
    who = require_scope(ctx, "execute")
    identifier(device, "device")
    if not isinstance(hours, int) or not 1 <= hours <= SILENCE_MAX_HOURS:
        raise BoundsError(f"hours: 1 to {SILENCE_MAX_HOURS}")
    reason = " ".join(str(reason or "").split())[:200]
    if len(reason) < 3:
        raise BoundsError("reason: a few words")
    requested_by = " ".join(str(requested_by or "").split())[:64]
    now = datetime.now(timezone.utc)
    ends = now + timedelta(hours=hours)
    created_by = f"{requested_by} via {who['user']}" if requested_by else who["user"]
    made = alertmanager.post("/api/v2/silences", json={
        "matchers": [{"name": "device", "value": device, "isRegex": False, "isEqual": True}],
        "startsAt": _iso(now), "endsAt": _iso(ends), "createdBy": created_by[:120], "comment": reason})
    return {"id": (made or {}).get("silenceID"), "device": device, "ends_at": _iso(ends), "created_by": created_by}


@mcp.tool()
def end_silence(silence_id: str, ctx: Context) -> dict:
    """End a silence before its time (its device's alerts notify again). Needs the execute scope.

    Args:
        silence_id: the id silence_device or list_silences gave.
    """
    who = require_scope(ctx, "execute")
    if not SILENCE_ID.fullmatch(silence_id or ""):
        raise BoundsError(f"invalid silence id: {silence_id!r}")
    alertmanager.delete(f"/api/v2/silence/{silence_id}")
    return {"id": silence_id, "ended": True, "by": who["user"]}
