"""Device changes — the rules, the protected lines and the store (the execution log). Offline, seconds.

One test per guarantee in docs/how-to/run-changes.md: a change reaches a device
only as it was staged (its fingerprint), once (an idempotency key; a retry gets
the first result), and only onto the configuration it was staged against. Who
may ask, and whether a person approved, is the connected platform's business:
nothing here looks at a role.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from automation.changes import lines as line_rules  # noqa: E402
from automation.changes import policy  # noqa: E402
from automation.changes.policy import Conflict, Refused, Settings  # noqa: E402
from automation.changes.store import NotFound, Store  # noqa: E402

LINES = ["interface Ethernet0/1", " no shutdown"]
ROLLBACK = ["interface Ethernet0/1", " shutdown"]
CONFIG = "! Last configuration change at 10:00:00 UTC\nhostname router1\ninterface Ethernet0/1\n shutdown\n"
T0 = 1_000_000.0
CONTROL = {"user": "platform-control", "sub": "svc-control", "via": "oidc", "agent": "platform-control"}
TOKEN = {"user": "execute-token", "sub": "execute-token", "via": "token", "agent": None}


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "changes.db", Settings(), clock=clock)


def stage(store, lines=LINES, rollback=ROLLBACK, actor=CONTROL, config=CONFIG):
    return store.stage(actor, "router1", lines, rollback, config, source="test")


def commit(store, change, key="key-0001", fp=None, actor=CONTROL):
    """begin_commit + a successful push, as the API does it."""
    claimed, replayed = store.begin_commit(change["id"], actor, key, fp if fp is not None else change["fingerprint"])
    if replayed:
        return claimed
    return store.finish(change["id"], result={"lines_sent": len(claimed["lines"]), "config_archived_to": "/x"})


# --- stage ---------------------------------------------------------------------

def test_stage_records_the_change_and_the_devices_configuration(store):
    change = stage(store)
    assert change["state"] == "staged"
    assert change["config_digest"] == policy.config_digest(CONFIG)
    assert change["fingerprint"] == policy.fingerprint("router1", LINES, ROLLBACK, change["config_digest"])
    assert change["requester"] == "platform-control"


def test_stage_says_which_lines_the_device_already_holds(store):
    change = stage(store, lines=["hostname router1", "interface Ethernet0/1", " description uplink"])
    assert change["already_present"] == ["hostname router1", "interface Ethernet0/1"]


def test_the_execute_token_can_stage_and_commit(store):
    """Standalone: no identity provider, the execute token runs the automation."""
    change = stage(store, actor=TOKEN)
    assert commit(store, change, actor=TOKEN)["state"] == "committed"


@pytest.mark.parametrize("smuggled", ["description x\nreload", "description x\rreload", "x" * 600])
def test_one_command_per_line(store, smuggled):
    with pytest.raises(Refused, match="one command per line"):
        stage(store, lines=["interface Ethernet0/1", smuggled], rollback=[])


def test_too_many_lines(tmp_path, clock):
    small = Store(tmp_path / "c.db", Settings(max_lines=1), clock=clock)
    with pytest.raises(Refused, match="more than 1 lines"):
        stage(small)


# --- commit: as staged, once ------------------------------------------------------

def test_commit_pushes_a_staged_change(store):
    done = commit(store, stage(store))
    assert done["state"] == "committed" and done["commit_key"] == "key-0001" and done["committed_at"]


def test_commit_needs_the_staged_fingerprint(store):
    change = stage(store)
    with pytest.raises(Conflict, match="fingerprint"):
        store.begin_commit(change["id"], CONTROL, "key-0001", "0" * 64)
    assert store.get(change["id"])["state"] == "staged", "a refused commit changes nothing"


def test_commit_needs_an_idempotency_key(store):
    change = stage(store)
    for key in ("", "short", "has space in it"):
        with pytest.raises(Refused, match="idempotency key"):
            store.begin_commit(change["id"], CONTROL, key, change["fingerprint"])


def test_a_retry_with_the_same_key_pushes_nothing(store):
    """A timeout made the caller retry: the first result comes back, no second push starts."""
    change = stage(store)
    first = commit(store, change)
    again, replayed = store.begin_commit(change["id"], CONTROL, "key-0001", change["fingerprint"])
    assert replayed and again["state"] == "committed" and again["committed_at"] == first["committed_at"]
    assert any(e["outcome"] == "replayed" for e in store.events(change_id=change["id"]))


def test_a_retry_while_the_push_runs_pushes_nothing(store):
    change = stage(store)
    store.begin_commit(change["id"], CONTROL, "key-0001", change["fingerprint"])
    _, replayed = store.begin_commit(change["id"], CONTROL, "key-0001", change["fingerprint"])
    assert replayed


def test_another_key_is_a_second_push_and_refused(store):
    change = stage(store)
    commit(store, change)
    with pytest.raises(Conflict, match="already committed"):
        store.begin_commit(change["id"], CONTROL, "key-0002", change["fingerprint"])


def test_two_commits_at_once_only_one_starts(store):
    change = stage(store)
    store.begin_commit(change["id"], CONTROL, "key-0001", change["fingerprint"])
    with pytest.raises(Conflict):
        store.begin_commit(change["id"], CONTROL, "key-0002", change["fingerprint"])


def test_a_changed_device_is_refused_and_the_change_closed(store):
    """The push found another configuration than the one staged against: nothing sent, the change must be staged
    again (the API raises DeviceChanged before sending; the store records it)."""
    change = stage(store)
    store.begin_commit(change["id"], CONTROL, "key-0001", change["fingerprint"])
    done = store.finish(change["id"], refused="router1: its configuration changed since the change was staged")
    assert done["state"] == "failed" and "changed since" in done["error"]
    assert "committed_at" not in done, "nothing was pushed"


def test_a_failed_push_is_recorded_and_never_retried(store):
    change = stage(store)
    store.begin_commit(change["id"], CONTROL, "key-0001", change["fingerprint"])
    done = store.finish(change["id"], error="router1: timed out")
    assert done["state"] == "failed"
    with pytest.raises(Conflict):
        store.begin_commit(change["id"], CONTROL, "key-0002", change["fingerprint"])


def test_an_interrupted_push_is_marked_failed_at_start(tmp_path, clock):
    first = Store(tmp_path / "c.db", Settings(), clock=clock)
    change = stage(first)
    first.begin_commit(change["id"], CONTROL, "key-0001", change["fingerprint"])
    again = Store(tmp_path / "c.db", Settings(), clock=clock)
    assert again.get(change["id"])["state"] == "failed"


# --- the device's configuration --------------------------------------------------

def test_the_digest_ignores_comments_and_timestamps():
    a = "! Last configuration change at 10:00:00\nhostname r1\nntp clock-period 17179869\n\ninterface Et0/1\n"
    b = "! Last configuration change at 11:30:00\nhostname r1\nntp clock-period 17179870\ninterface Et0/1   \n"
    assert policy.config_digest(a) == policy.config_digest(b)
    assert policy.config_digest("# 2026-11-16 by RouterOS\n/ip address\n") == \
        policy.config_digest("# 2026-11-17 by RouterOS\n/ip address\n")


def test_the_digest_sees_a_real_change():
    assert policy.config_digest("hostname r1\ninterface Et0/1\n shutdown\n") != \
        policy.config_digest("hostname r1\ninterface Et0/1\n no shutdown\n")


def test_the_fingerprint_binds_the_configuration():
    assert policy.fingerprint("r1", LINES, ROLLBACK, "a") != policy.fingerprint("r1", LINES, ROLLBACK, "b")


# --- rollback and withdraw ---------------------------------------------------------

def test_rollback_once(store):
    change = stage(store)
    commit(store, change)
    claimed, replayed = store.begin_commit(change["id"], CONTROL, "back-0001", rollback=True)
    assert not replayed and claimed["rollback_used_at"]
    store.finish(change["id"], result={"lines_sent": 2}, rollback=True)
    _, replayed = store.begin_commit(change["id"], CONTROL, "back-0001", rollback=True)
    assert replayed, "the same key repeats the rollback's answer"
    with pytest.raises(Conflict, match="already rolled back"):
        store.begin_commit(change["id"], CONTROL, "back-0002", rollback=True)


def test_rollback_needs_a_pushed_change_with_a_rollback(store):
    change = stage(store)
    with pytest.raises(Conflict, match="only a pushed change"):
        store.begin_commit(change["id"], CONTROL, "back-0001", rollback=True)
    bare = stage(store, rollback=[])
    commit(store, bare, key="key-0009")
    with pytest.raises(Refused, match="declared no rollback"):
        store.begin_commit(bare["id"], CONTROL, "back-0001", rollback=True)


def test_withdraw_only_a_staged_change(store):
    change = stage(store)
    assert store.withdraw(change["id"], CONTROL)["state"] == "withdrawn"
    with pytest.raises(Conflict):
        store.withdraw(change["id"], CONTROL)
    with pytest.raises(Conflict):
        store.begin_commit(change["id"], CONTROL, "key-0001", change["fingerprint"])


# --- the execution log -------------------------------------------------------------

def test_every_action_and_refusal_is_logged_with_the_caller_and_agent(store):
    change = stage(store)
    with pytest.raises(Conflict):
        store.begin_commit(change["id"], CONTROL, "key-0001", "0" * 64)
    commit(store, change)
    events = store.events(change_id=change["id"])
    assert [e["action"] + ":" + e["outcome"] for e in reversed(events)] == [
        "stage:ok", "commit:refused", "commit:started", "commit:ok"]
    assert all(e["user"] == "platform-control" and e["agent"] == "platform-control" for e in events)


def test_a_refused_stage_is_logged(store):
    with pytest.raises(Refused):
        stage(store, lines=["reload"])
    assert store.events()[0]["action"] == "stage" and store.events()[0]["outcome"] == "refused"


def test_unknown_change(store):
    with pytest.raises(NotFound):
        store.get("0000-0000")


def test_1x_changes_still_open_are_closed_at_start(tmp_path, clock):
    """A change waiting or approved under 1.x is never committed under 2.0.0: its approval followed rules now gone."""
    import json
    import sqlite3
    first = Store(tmp_path / "c.db", Settings(), clock=clock)
    change = stage(first)
    db = sqlite3.connect(str(tmp_path / "c.db"))
    for state in ("waiting", "approved"):
        old = {**change, "id": f"{state[:4]}-0001", "state": state}
        db.execute("INSERT INTO changes (id, state, staged_at, data) VALUES (?, ?, ?, ?)",
                   (old["id"], state, T0, json.dumps(old)))
    db.commit()
    again = Store(tmp_path / "c.db", Settings(), clock=clock)
    for cid in ("wait-0001", "appr-0001"):
        closed = again.get(cid)
        assert closed["state"] == "withdrawn" and "2.0.0" in closed["error"]
    assert again.get(change["id"])["state"] == "staged", "a 2.0.0 change is left as it is"


def test_recent_states_name_each_change_and_nothing_else(store, clock):
    change = stage(store)
    assert store.recent_states() == {change["id"]: "staged"}
    clock.now += 86401
    assert store.recent_states() == {}, "older than a day: no longer exported"


def test_counts_per_state_include_zeros(store):
    stage(store)
    counts = store.counts()
    assert counts["staged"] == 1 and counts["committed"] == 0 and set(counts) == set(policy.STATES)


# --- protected lines -----------------------------------------------------------

@pytest.mark.parametrize("line, rule", [
    ("reload", "reload"), ("reload in 5", "reload"), ("/system reboot", "reload"),
    ("write erase", "erase-configuration"), ("erase startup-config", "erase-configuration"),
    ("reset saved-configuration", "erase-configuration"),
    ("no router bgp 65000", "remove-routing-process"), ("undo bgp", "remove-routing-process"),
    ("username admin privilege 15 secret x", "local-users-and-aaa"), ("no aaa new-model", "local-users-and-aaa"),
    ("local-user admin", "local-users-and-aaa"), ("/user add name=x", "local-users-and-aaa"),
])
def test_protected_lines_are_refused(line, rule):
    found = line_rules.refusals([line])
    assert found and rule in found[0]


@pytest.mark.parametrize("lines", [
    ["interface Ethernet0/1", " no shutdown"], ["router bgp 65000", " neighbor 192.0.2.1 shutdown"],
    ["interface Ethernet0/1", " description uplink to hq"], ["ip route 0.0.0.0 0.0.0.0 192.0.2.1"],
])
def test_ordinary_changes_pass(lines):
    assert line_rules.refusals(lines) == []


def test_a_protected_line_is_refused_at_staging_and_named(store):
    with pytest.raises(Refused, match=r"line 2 .*no router bgp 65000.*remove-routing-process"):
        stage(store, ["router bgp 65000", "no router bgp 65000"], [])


def test_the_rollback_is_checked_too(store):
    with pytest.raises(Refused, match="rollback line 1"):
        stage(store, LINES, ["reload"])


def test_the_management_section_left_the_rule_file(tmp_path):
    """Which interfaces carry management access is the connected platform's policy since 2.0.0: an old rule file that
    still names it fails loudly rather than seeming to protect them."""
    old = tmp_path / "rules.yml"
    old.write_text("rules: []\nmanagement:\n  inside_match: '^shutdown'\n")
    with pytest.raises(line_rules.LineRuleError, match="moved to the connected platform"):
        line_rules.refusals(["anything"], path=str(old))


def test_a_malformed_rule_file_fails_loudly(tmp_path):
    bad = tmp_path / "rules.yml"
    bad.write_text("rules:\n  - name: x\n    match: '('\n")
    with pytest.raises(line_rules.LineRuleError):
        line_rules.refusals(["anything"], path=str(bad))


