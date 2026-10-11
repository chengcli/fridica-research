"""One SQLite file: `meta(key, value)` and `studies(thread_id, state_json, updated_at)`.

The snapshot written after every step equals the fold of `step` over the events (the machine
is pure), so there is no event log and no actions table. `meta` holds the feed cursor, owner
commands from the CLI (`cmd:<thread>`), and the board's card ids (`board:<thread>`).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .machine import State

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS studies (thread_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at REAL NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:": Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
        self.db.executescript(SCHEMA)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str | None):
        if value is None: self.db.execute("DELETE FROM meta WHERE key=?", (key,))
        else: self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @property
    def cursor(self) -> int | None:
        """None until the first poll: a fresh driver starts at the ledger's end, not at record 0."""
        v = self.get_meta("cursor")
        return int(v) if v is not None else None

    def save(self, state: State, cursor: int | None = None, now: float | None = None):
        """One transaction: the thread's snapshot and (optionally) the advanced cursor."""
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("INSERT INTO studies(thread_id,state_json,updated_at) VALUES(?,?,?) ON CONFLICT(thread_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at", (state.thread, json.dumps(state.to_dict(), sort_keys=True), now or time.time()))
            if cursor is not None: self.set_meta("cursor", str(cursor))

    def set_cursor(self, cursor: int): self.set_meta("cursor", str(cursor))

    def load(self, thread: str) -> State | None:
        row = self.db.execute("SELECT state_json FROM studies WHERE thread_id=?", (thread,)).fetchone()
        return State.from_dict(json.loads(row[0])) if row else None

    def all(self) -> list[State]:
        return [State.from_dict(json.loads(r[0])) for r in self.db.execute("SELECT state_json FROM studies ORDER BY updated_at")]

    def rounds(self) -> list[str]:
        """Threads adopted as people-run rounds (#46): `round:<thread>` meta keys, never rows of `studies`."""
        return [r[0].split(":", 1)[1] for r in self.db.execute("SELECT key FROM meta WHERE key LIKE 'round:%' ORDER BY key")]

    def command(self, thread: str, cmd: str | None = None) -> str | None:
        """Owner commands from the CLI to the serving driver: `cmd` appends one; none pops the oldest."""
        key = f"cmd:{thread}"
        queue = json.loads(self.get_meta(key) or "[]")
        if cmd is not None:
            self.set_meta(key, json.dumps(queue + [cmd]))
            return cmd
        if not queue: return None
        self.set_meta(key, json.dumps(queue[1:]) if queue[1:] else None)
        return queue[0]

    def close(self): self.db.close()
