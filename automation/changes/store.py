"""The changes, and every action on them, in one SQLite file: the execution log.

SQLite because the automation API runs ONE worker (see scheduler.py) and a
change is a few rows: no server to run, and the file lives on a named volume so
a container rebuild keeps the history. Every state transition happens under one
lock, so two commits of the same change cannot both start.

Every action is an event, done or refused, with the caller and the agent.
The store never reaches a device: the API reads the configuration and pushes,
and hands the store what it needs (the configuration's digest).
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import lines as line_rules
from . import policy
from .policy import COMMITTED, COMMITTING, FAILED, LEGACY_OPEN, STAGED, WITHDRAWN, Conflict, Refused

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, state TEXT NOT NULL, staged_at REAL NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS changes_state ON changes(state, staged_at);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, change_id TEXT,
  action TEXT NOT NULL, outcome TEXT NOT NULL, user TEXT, agent TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_t ON events(t);
"""

STACK = {"user": "stack"}


class NotFound(LookupError):
    pass


def _who(actor: dict) -> tuple[str, str | None]:
    return str(actor.get("user") or "?"), actor.get("agent")


class Store:
    def __init__(self, path: str | Path, settings: policy.Settings | None = None, clock=time.time):
        self.settings = settings or policy.Settings.from_env()
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        with self._lock:
            # A push interrupted by a restart: the device may or may not have
            # it. Never retried by itself; a person looks at the device.
            for change in self._select("state = ?", (COMMITTING,)):
                self._finish(change, FAILED, error="interrupted by a restart: check the device before retrying")
            # A 1.x change still waiting or approved: its approval followed the 1.x rules, which are gone.
            for state in LEGACY_OPEN:
                for change in self._select("state = ?", (state,)):
                    change.update(state=WITHDRAWN, withdrawn_at=self.clock(),
                                  error=f"closed by the move to 2.0.0 (was {state}): stage it again")
                    self._save(change)
                    self._event(change["id"], "withdraw", "ok", STACK, f"was {state} under 1.x")

    # --- reading ------------------------------------------------------------

    def get(self, change_id: str) -> dict:
        with self._lock:
            return self._get(change_id)

    def list(self, state: str | None = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if state:
                return self._select("state = ? ORDER BY staged_at DESC LIMIT ?", (state, limit))
            return self._select("1 ORDER BY staged_at DESC LIMIT ?", (limit,))

    def counts(self) -> dict[str, int]:
        """How many changes are in each state: what /metrics exports."""
        with self._lock:
            found = dict(self._db.execute("SELECT state, COUNT(*) FROM changes GROUP BY state").fetchall())
        return {state: int(found.get(state, 0)) for state in policy.STATES}

    def recent_states(self, within: float = 86400.0) -> dict[str, str]:
        """Each change staged within `within` seconds, by id, with its state: what /metrics exports per change. Only
        the id and the state: never the device, the lines or a person."""
        with self._lock:
            rows = self._db.execute("SELECT id, state FROM changes WHERE staged_at >= ?",
                                    (self.clock() - within,)).fetchall()
        return {change_id: state for change_id, state in rows}

    def events(self, limit: int = 200, change_id: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        where, args = ("WHERE change_id = ?", (change_id, limit)) if change_id else ("", (limit,))
        rows = self._db.execute(
            f"SELECT t, change_id, action, outcome, user, agent, detail FROM events {where} ORDER BY id DESC LIMIT ?",
            args).fetchall()
        keys = ("t", "change_id", "action", "outcome", "user", "agent", "detail")
        return [dict(zip(keys, row)) for row in rows]

    # --- acting -------------------------------------------------------------

    def check_lines(self, actor: dict, device: str, lines: list[str], rollback: list[str]) -> None:
        """Everything about a change that needs no device: shape and protected lines. Refusals are logged."""
        try:
            policy.check_stage(device, lines, rollback, self.settings)
            protected = line_rules.refusals(lines) + [f"rollback {r}" for r in line_rules.refusals(rollback)]
            if protected:
                raise Refused("protected lines: " + "; ".join(protected))
        except Refused as exc:
            with self._lock:
                self._event(None, "stage", "refused", actor, f"{device}: {exc}")
            raise

    def stage(self, actor: dict, device: str, lines: list[str], rollback: list[str], config: str,
              source: str = "") -> dict:
        """Record a change against the device's configuration as read just now (`config`). Nothing is sent."""
        self.check_lines(actor, device, lines, rollback)
        now = self.clock()
        digest = policy.config_digest(config)
        user, agent = _who(actor)
        with self._lock:
            change = {
                "id": self._new_id(), "device": device, "lines": lines, "rollback": rollback,
                "config_digest": digest, "fingerprint": policy.fingerprint(device, lines, rollback, digest),
                "already_present": policy.already_present(config, lines), "state": STAGED,
                "source": source[:64], "requester": user, "requester_sub": actor.get("sub"),
                "requester_agent": agent, "requester_platform": actor.get("platform"), "staged_at": now,
            }
            self._db.execute("INSERT INTO changes (id, state, staged_at, data) VALUES (?, ?, ?, ?)",
                             (change["id"], STAGED, now, json.dumps(change)))
            self._event(change["id"], "stage", "ok", actor, f"{device}: {len(lines)} lines")
            return change

    def withdraw(self, change_id: str, actor: dict) -> dict:
        with self._lock:
            change = self._get(change_id)
            self._check(lambda: policy.check_withdraw(change), change, actor, "withdraw")
            change.update(state=WITHDRAWN, withdrawn_at=self.clock())
            self._save(change)
            self._event(change_id, "withdraw", "ok", actor, "")
            return change

    def begin_commit(self, change_id: str, actor: dict, key: str, fingerprint: str = "",
                     rollback: bool = False) -> tuple[dict, bool]:
        """Claim the change for one push. Returns (change, replayed): replayed means this key already did it, so the
        caller answers with the change as it is and pushes nothing. Otherwise the caller pushes, then finish()."""
        action = "rollback" if rollback else "commit"
        with self._lock:
            change = self._get(change_id)

            def checks():
                policy.check_key(key)
                if policy.replay(change, key, rollback):
                    return
                policy.check_rollback(change) if rollback else policy.check_commit(change, fingerprint)

            self._check(checks, change, actor, action)
            if change.get("rollback_key" if rollback else "commit_key") == key:
                self._event(change_id, action, "replayed", actor, "same idempotency key: nothing pushed")
                return change, True
            user, agent = _who(actor)
            if rollback:
                change.update(rollback_key=key, rollback_used_at=self.clock(), rollback_by=user, rollback_agent=agent)
            else:
                change.update(state=COMMITTING, commit_key=key, committer=user, committer_agent=agent,
                              commit_started_at=self.clock())
            self._save(change)
            self._event(change_id, action, "started", actor, "")
            return change, False

    def finish(self, change_id: str, result: dict | None = None, error: str = "", rollback: bool = False,
               refused: str = "") -> dict:
        """Record how the push ended. `refused`: nothing was sent (the device's configuration had changed since the
        stage); the change is closed as failed with the reason, to be staged again against the new configuration."""
        with self._lock:
            change = self._get(change_id)
            if rollback:
                change.update(rollback_result=result, rollback_error=(refused or error)[:500])
                self._save(change)
                self._event(change_id, "rollback", "ok" if not (error or refused) else "failed",
                            {"user": change.get("rollback_by"), "agent": change.get("rollback_agent")},
                            (refused or error)[:500])
                return change
            return self._finish(change, FAILED if (error or refused) else COMMITTED, result=result,
                                error=refused or error, pushed=not refused)

    # --- internals ----------------------------------------------------------

    def _check(self, check, change: dict, actor: dict, action: str) -> None:
        try:
            check()
        except Refused as exc:
            self._event(change["id"], action, "refused", actor, str(exc))
            raise

    def _finish(self, change: dict, state: str, result: dict | None = None, error: str = "",
                pushed: bool = True) -> dict:
        now = self.clock()
        change.update(state=state, result=result, error=error[:500])
        if pushed:
            change["committed_at"] = now
        self._save(change)
        self._event(change["id"], "commit", "ok" if state == COMMITTED else "failed",
                    {"user": change.get("committer"), "agent": change.get("committer_agent")}, error[:500])
        return change

    def _new_id(self) -> str:
        while True:
            raw = secrets.token_hex(4)
            change_id = f"{raw[:4]}-{raw[4:]}"
            if not self._db.execute("SELECT 1 FROM changes WHERE id = ?", (change_id,)).fetchone():
                return change_id

    def _get(self, change_id: str) -> dict:
        row = self._db.execute("SELECT data FROM changes WHERE id = ?", (change_id,)).fetchone()
        if not row:
            raise NotFound(f"no change {change_id!r}")
        return json.loads(row[0])

    def _select(self, where: str, args: tuple) -> list[dict]:
        rows = self._db.execute(f"SELECT data FROM changes WHERE {where}", args).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _save(self, change: dict) -> None:
        self._db.execute("UPDATE changes SET state = ?, data = ? WHERE id = ?",
                         (change["state"], json.dumps(change), change["id"]))

    def _event(self, change_id: str | None, action: str, outcome: str, actor: dict, detail: str) -> None:
        user, agent = _who(actor)
        self._db.execute("INSERT INTO events (t, change_id, action, outcome, user, agent, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (self.clock(), change_id, action, outcome, user, agent, detail))


__all__ = ["Store", "NotFound", "Refused", "Conflict"]
