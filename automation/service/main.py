"""The automation API — get and put information and configuration on devices.

Every device operation in the stack goes through here, including the ones an AI
platform calls: mcp-netmiko, mcp-assurance and mcp-pyats front THIS API rather than opening
their own SSH sessions. That keeps credentials, TextFSM parsing and error
handling in exactly one place.

The same functions are importable by the scripts in automation/netmiko/, so
nothing is duplicated between CLI and HTTP.
"""
from __future__ import annotations

import os
import re
import threading
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

# Absolute imports only — automation/nornir/ and automation/textfsm/ shadow the
# installed libraries if this package is imported any other way.
from automation.assurance import normalise
from automation.changes.policy import Conflict, Refused
from automation.changes.store import NotFound, Store
from automation.nornir import tasks
from automation.service import scheduler
from automation.textfsm import parse as textfsm_parse
from automation.ttp import parse as ttp_parse

# A device change is staged, then committed once (automation/changes/). There is no direct push: every write goes
# through the execution log. docs/how-to/run-changes.md
CHANGES_DB = os.environ.get("CHANGES_DB", "/var/lib/automation/changes.db")

app = FastAPI(
    title="Cubiq Automation",
    description="Get and put information and configuration on network devices.",
    version="1.0",
)

# Device names come from Infrahub, but a name still reaches this API from an
# untrusted caller — an AI platform through mcp-netmiko, for instance — and is
# used to build CLI commands. Bound it before it goes anywhere near a device.
DEVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def check_device_name(device: str) -> str:
    if not DEVICE_RE.fullmatch(device):
        raise HTTPException(400, f"invalid device name: {device!r}")
    return device




@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.on_event("startup")
def start_scheduled_assurance():
    # Off unless ASSURANCE_INTERVAL_MINUTES > 0 — see automation/service/scheduler.py.
    scheduler.start()


@app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def metrics():
    """Scheduled assurance results and the changes per state, in Prometheus text format."""
    lines = ["# HELP changes Device changes per state (staged, then committed once).",
             "# TYPE changes gauge"]
    lines += [f'changes{{state="{state}"}} {count}' for state, count in changes().counts().items()]
    # One series per change staged in the last 24 h: its state, for whoever follows it. The ids
    # are the store's own (four hex, a dash, four hex), so they need no escaping.
    lines += ["# HELP change_state The state of each change staged in the last 24 hours (1 = its current state).",
              "# TYPE change_state gauge"]
    lines += [f'change_state{{change_id="{cid}",state="{state}"}} 1'
              for cid, state in sorted(changes().recent_states().items())]
    return scheduler.metrics() + "\n".join(lines) + "\n"


@app.get("/assurance/latest")
def assurance_latest():
    """The latest scheduled assurance results, with each rule's detail."""
    return scheduler.latest()


@app.get("/devices")
def devices():
    """Every device Infrahub knows about, as the automation layer sees it."""
    try:
        return {"devices": tasks.list_devices()}
    except Exception as exc:
        raise HTTPException(502, f"inventory unavailable: {exc}") from exc


@app.get("/device/{device}/config")
def device_config(device: str):
    """Fetch the running configuration. A copy is archived under configs/."""
    check_device_name(device)
    try:
        return tasks.get_config(device)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.get("/device/{device}/state")
def device_state(
    device: str,
    command: str | None = Query(None, description="Override the platform's default state command"),
    raw: bool = Query(False, description="Return unparsed CLI output"),
    view: str | None = Query(None, pattern="^(bgp|ospf)$", description="bgp or ospf: the platform's fixed command"),
):
    """Operational state, parsed into rows by TextFSM.

    `raw=1` returns the unparsed output — that is how you capture a new sample
    for automation/textfsm/samples/ when a template needs writing.
    """
    check_device_name(device)
    try:
        return tasks.get_state(device, command=None if view else command, raw=raw, view=view)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except textfsm_parse.ParseError as exc:
        # 422, not 500: the device answered, we could not parse it. The message
        # names the template gap and points at the runbook.
        raise HTTPException(
            422,
            f"{exc} — capture the output with ?raw=1 and see docs/how-to/add-a-textfsm-template.md",
        ) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.post("/device/{device}/check")
def device_check(device: str):
    """Run the assurance rules against a device.

    Vendor-neutral: the same rules in automation/assurance/rules.yml run for
    Cisco, Huawei and MikroTik, because parsed output is normalised to one
    shape first. Add a rule by editing that file — no code change.
    """
    check_device_name(device)
    started = time.time()
    try:
        result = tasks.run_assurance(device)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (textfsm_parse.ParseError, normalise.NormaliseError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc
    # The exported state follows the newest check, scheduled or not (scheduler.record).
    scheduler.record(device, result, started)
    return result


@app.get("/device/{device}/snapshot")
def device_snapshot(device: str):
    """A comparable point-in-time view of a device, for pre/post comparison."""
    check_device_name(device)
    try:
        return tasks.snapshot_device(device)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (textfsm_parse.ParseError, normalise.NormaliseError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.get("/device/{device}/config/structured")
def device_config_structured(device: str, kind: str = Query("interfaces")):
    """Running config parsed into structure with TTP.

    TextFSM parses tabular `show` output; config is hierarchical, which is what
    TTP handles. See automation/ttp/templates/.
    """
    check_device_name(device)
    try:
        return tasks.get_config_structured(device, kind=kind)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ttp_parse.TTPParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


# --- changes: stage, then commit once ----------------------------------------
# The caller and agent on every call come from the MCP layer, which verified the
# credential and its execute scope (mcp-netmiko). This API trusts that, so it must
# not be reachable from outside the host: AUTOMATION_BIND_IP defaults to loopback.

_store: Store | None = None
_store_lock = threading.Lock()


def changes() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = Store(CHANGES_DB)
        return _store


class Actor(BaseModel):
    user: str = Field(..., max_length=128)
    sub: str = Field(..., max_length=128)
    via: str = Field(..., max_length=16)
    agent: str | None = Field(None, max_length=128)


class StageBody(BaseModel):
    actor: Actor
    device: str
    lines: list[str] = Field(..., min_length=1)
    rollback: list[str] = Field(default_factory=list)
    source: str = Field("", max_length=64)


class ActBody(BaseModel):
    actor: Actor


class PushBody(BaseModel):
    actor: Actor
    idempotency_key: str = Field(..., max_length=128)
    fingerprint: str = Field("", max_length=64)


def _change_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except Refused as exc:
        raise HTTPException(403, str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/changes", status_code=201)
def change_stage(body: StageBody):
    """Stage a change: checked, then fingerprinted with the device's configuration as read now (the dry run: which
    lines it already holds). Nothing is sent to the device."""
    check_device_name(body.device)
    actor = body.actor.model_dump()
    # The lines first: a refused change costs no device session.
    _change_call(changes().check_lines, actor, body.device, body.lines, body.rollback)
    try:
        config = tasks.get_config(body.device)["config"]
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{body.device}: cannot read its configuration to stage against: {exc}") from exc
    return _change_call(changes().stage, actor, body.device, body.lines, body.rollback, config, source=body.source)


@app.get("/changes")
def change_list(state: str | None = Query(None, max_length=16), limit: int = Query(100, ge=1, le=500)):
    return {"changes": changes().list(state=state, limit=limit)}


@app.get("/changes/events")
def change_events(change_id: str | None = Query(None, max_length=16), limit: int = Query(200, ge=1, le=1000)):
    """Every action on changes, done or refused, newest first: the execution log."""
    return {"events": changes().events(limit=limit, change_id=change_id)}


@app.get("/changes/{change_id}")
def change_get(change_id: str):
    return _change_call(changes().get, change_id)


@app.post("/changes/{change_id}/withdraw")
def change_withdraw(change_id: str, body: ActBody):
    return _change_call(changes().withdraw, change_id, body.actor.model_dump())


def _push(change_id: str, body: PushBody, rollback: bool) -> dict:
    store = changes()
    change, replayed = _change_call(store.begin_commit, change_id, body.actor.model_dump(), body.idempotency_key,
                                    body.fingerprint, rollback=rollback)
    if replayed:
        return change
    lines = change["rollback"] if rollback else change["lines"]
    # A commit pushes onto the configuration it was staged against, or not at all. A rollback undoes a push, whatever
    # happened since.
    expect = None if rollback else change.get("config_digest")
    try:
        result = tasks.put_config(change["device"], lines, expect_digest=expect)
    except tasks.DeviceChanged as exc:
        done = store.finish(change_id, refused=str(exc), rollback=rollback)
        raise HTTPException(409, {"error": str(exc), "change": done}) from exc
    except Exception as exc:  # the push failed or may be partial: recorded, never retried by itself
        done = store.finish(change_id, error=f"{change['device']}: {exc}", rollback=rollback)
        raise HTTPException(502, {"error": str(exc), "change": done}) from exc
    return store.finish(change_id, result=result, rollback=rollback)


@app.post("/changes/{change_id}/commit")
def change_commit(change_id: str, body: PushBody):
    """Push a staged change, once: its fingerprint and an idempotency key. The previous config is archived first."""
    return _push(change_id, body, rollback=False)


@app.post("/changes/{change_id}/rollback")
def change_rollback(change_id: str, body: PushBody):
    """Push a committed change's declared rollback, once (an idempotency key)."""
    return _push(change_id, body, rollback=True)


# --- pyATS / Genie ---------------------------------------------------------
# Read only. A feature must be in the platform's `pyats:` allow-list in
# platforms.yml — there is no "run this Genie command" endpoint, for the same
# reason there is no raw-CLI one.

FEATURE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@app.get("/device/{device}/pyats/features")
def device_pyats_features(device: str):
    """What pyATS/Genie can return for this device's platform."""
    check_device_name(device)
    try:
        return tasks.pyats_features(device)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.get("/device/{device}/pyats/learn/{feature}")
def device_pyats_learn(device: str, feature: str):
    """Genie's structured model of one feature: status ok, absent or error."""
    check_device_name(device)
    if not FEATURE_RE.fullmatch(feature):
        raise HTTPException(400, f"invalid feature name: {feature!r}")
    try:
        return tasks.pyats_learn(device, feature)
    except tasks.DeviceError as exc:
        raise HTTPException(400 if "not a pyATS feature" in str(exc) else 404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.get("/device/{device}/pyats/bgp")
def device_pyats_bgp(device: str):
    """BGP sessions as {vrf, af, peer, state}, from Genie."""
    check_device_name(device)
    try:
        return tasks.pyats_bgp_neighbors(device)
    except tasks.DeviceError as exc:
        raise HTTPException(400 if "not a pyATS feature" in str(exc) else 404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc
