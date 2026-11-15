"""Runs INSIDE darqcube/automation:local, driven by test_changes_api.py.

The automation API's /changes endpoints with the real store and rules; only
the device (its configuration and the push) is replaced. Pinned:

- the raw one-step push is gone (no route)
- stage reads the device's configuration and pushes nothing; commit with the
  staged fingerprint and an idempotency key pushes exactly the staged lines
- a retry with the same key pushes nothing; another key is a 409
- a wrong fingerprint is a 409, and nothing is pushed
- a device whose configuration changed since the stage is a 409, nothing sent
- a protected line is refused at staging (403) without reading the device;
  an unknown device is 404
- a push that fails is recorded and never retried
- every action, done or refused, is in /changes/events

Prints one JSON object: {check name: [passed, detail]}.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback

os.environ.update({"CHANGES_DB": os.path.join(tempfile.mkdtemp(), "changes.db")})
sys.path.insert(0, "/app")

from fastapi.testclient import TestClient  # noqa: E402

from automation.changes.policy import config_digest  # noqa: E402
from automation.nornir import tasks  # noqa: E402
from automation.service import main  # noqa: E402

CONFIGS = {"router1": "! Last configuration change at 10:00\nhostname router1\ninterface Ethernet0/1\n shutdown\n",
           "router2": "hostname router2\n"}
reads: list[str] = []
pushed: list[tuple[str, list[str]]] = []
fail_next = {"on": False}
results: dict[str, list] = {}


def fake_get_config(device, nr=None):
    if device not in CONFIGS:
        raise tasks.DeviceError(f"device '{device}' is not in Infrahub")
    reads.append(device)
    return {"device": device, "config": CONFIGS[device], "saved_to": f"configs/{device}.cfg"}


def fake_put_config(device, lines, expect_digest=None):
    if expect_digest is not None and config_digest(CONFIGS[device]) != expect_digest:
        raise tasks.DeviceChanged(f"{device}: its configuration changed since the change was staged; nothing was sent.")
    if fail_next["on"]:
        fail_next["on"] = False
        raise tasks.DeviceError(f"{device}: unreachable")
    pushed.append((device, list(lines)))
    return {"device": device, "lines_sent": len(lines), "config_archived_to": f"configs/{device}.cfg", "output": ""}


tasks.get_config = fake_get_config
tasks.put_config = fake_put_config

CONTROL = {"user": "platform-control", "sub": "svc-control", "via": "oidc", "agent": "platform-control"}
LINES = ["interface Ethernet0/1", " no shutdown"]


def check(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception:
        ok, detail = False, traceback.format_exc()[-600:]
    results[name] = [bool(ok), str(detail)[:600]]


with TestClient(main.app) as c:
    def stage(lines=LINES, device="router1"):
        return c.post("/changes", json={"actor": CONTROL, "device": device, "lines": lines,
                                        "rollback": ["interface Ethernet0/1", " shutdown"], "source": "chat"})

    def commit(change, key="key-0001", fp=None):
        return c.post(f"/changes/{change['id']}/commit", json={
            "actor": CONTROL, "idempotency_key": key, "fingerprint": change["fingerprint"] if fp is None else fp})

    def raw_push_gone():
        r = c.post("/device/router1/config", json={"lines": ["x"]})
        return r.status_code in (404, 405) and not pushed, (r.status_code, r.text[:200])
    check("the_raw_push_is_gone", raw_push_gone)

    def stage_then_commit():
        pushed.clear()
        r = stage()
        change = r.json()
        nothing_yet = not pushed
        m = commit(change)
        ok = (r.status_code == 201 and nothing_yet and change["already_present"] == ["interface Ethernet0/1"]
              and m.status_code == 200 and m.json()["state"] == "committed" and pushed == [("router1", LINES)])
        return ok, (r.status_code, m.status_code, pushed, change.get("already_present"))
    check("stage_pushes_nothing_and_commit_pushes_exactly_the_staged_lines", stage_then_commit)

    def same_key_twice():
        pushed.clear()
        change = stage().json()
        first = commit(change)
        again = commit(change)
        other = commit(change, key="key-0002")
        ok = (first.status_code == again.status_code == 200 and again.json()["state"] == "committed"
              and other.status_code == 409 and len(pushed) == 1)
        return ok, (first.status_code, again.status_code, other.status_code, len(pushed))
    check("a_retry_with_the_same_key_pushes_nothing_and_another_key_is_409", same_key_twice)

    def wrong_fingerprint():
        pushed.clear()
        change = stage().json()
        r = commit(change, fp="0" * 64)
        return r.status_code == 409 and "fingerprint" in r.text and not pushed, (r.status_code, r.text[:200])
    check("a_wrong_fingerprint_is_409_and_pushes_nothing", wrong_fingerprint)

    def device_changed():
        pushed.clear()
        change = stage().json()
        saved = CONFIGS["router1"]
        CONFIGS["router1"] = saved.replace(" shutdown", " description someone was here")
        try:
            r = commit(change)
        finally:
            CONFIGS["router1"] = saved
        state = c.get(f"/changes/{change['id']}").json()["state"]
        return r.status_code == 409 and "changed since" in r.text and not pushed and state == "failed", \
            (r.status_code, state, r.text[:200])
    check("a_device_changed_since_the_stage_is_409_and_nothing_is_sent", device_changed)

    def timestamps_are_not_a_change():
        pushed.clear()
        change = stage().json()
        saved = CONFIGS["router1"]
        CONFIGS["router1"] = saved.replace("10:00", "11:45")
        try:
            r = commit(change)
        finally:
            CONFIGS["router1"] = saved
        return r.status_code == 200 and len(pushed) == 1, (r.status_code, r.text[:200])
    check("a_new_timestamp_comment_is_not_a_device_change", timestamps_are_not_a_change)

    def protected():
        reads.clear()
        r = stage(lines=["reload"])
        return r.status_code == 403 and "reload" in r.text and not reads, (r.status_code, reads, r.text[:200])
    check("a_protected_line_is_403_before_the_device_is_read", protected)

    def unknown_device():
        r = stage(device="nowhere")
        return r.status_code == 404, r.status_code
    check("an_unknown_device_is_404", unknown_device)

    def failed_push():
        pushed.clear()
        change = stage().json()
        fail_next["on"] = True
        r = commit(change)
        again = commit(change, key="key-0002")
        state = c.get(f"/changes/{change['id']}").json()["state"]
        return r.status_code == 502 and state == "failed" and again.status_code == 409 and not pushed, \
            (r.status_code, state, again.status_code)
    check("a_failed_push_is_recorded_and_not_retried", failed_push)

    def rollback_once():
        pushed.clear()
        change = stage().json()
        commit(change)
        body = {"actor": CONTROL, "idempotency_key": "back-0001"}
        first = c.post(f"/changes/{change['id']}/rollback", json=body)
        again = c.post(f"/changes/{change['id']}/rollback", json=body)
        ok = first.status_code == again.status_code == 200 and pushed[-1] == ("router1", change["rollback"]) \
            and len(pushed) == 2
        return ok, (first.status_code, again.status_code, len(pushed))
    check("a_rollback_pushes_once", rollback_once)

    def events():
        r = c.get("/changes/events?limit=500").json()["events"]
        outcomes = {(e["action"], e["outcome"]) for e in r}
        ok = {("stage", "ok"), ("stage", "refused"), ("commit", "ok"), ("commit", "refused"), ("commit", "replayed"),
              ("commit", "failed"), ("rollback", "ok")} <= outcomes
        return ok, sorted(outcomes)
    check("every_action_done_or_refused_is_an_event", events)

    def listing():
        r = c.get("/changes?state=committed").json()["changes"]
        return r and all(x["state"] == "committed" for x in r), len(r)
    check("changes_list_by_state", listing)

print(json.dumps(results))
