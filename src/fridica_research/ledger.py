"""R49/R52: the job ledger (#53) — `innies`, `jobs`, `workload`, `lessons`, `waves`.

One SQLite file holding one row per job, who did it, what the ETA rested on and how it came out,
so the floor manager's ETAs can be scored (R48) and a wave can be closed with a record (R52).

`DDL` below is the prototype schema, copied verbatim from Cheng's comment on issue #53
(comment id 6113435186, 2026-10-11T20:31:57Z); `tests/test_issue53_ledger.py` checks the
database this module creates against that text column by column. The real prototype
(`~/.local/state/fridica-research/jobs.sqlite3`) stays with the floor manager: nothing here
reads a fixed path, and the tests run on a synthetic fixture built from the DDL.

The importer is **idempotent**. Importing the same records a second time leaves every table
byte-identical, which is what lets the floor manager re-run an import over a prototype that has
grown rows since, without touching the rows already carried over:

- rows are matched on each table's primary key (`innies.innie`, `jobs.id`, `lessons.id`,
  `waves.wave`, `workload.id`);
- a row that is absent is inserted, a row whose supplied columns all match is left alone, and a
  row that genuinely differs is updated on the supplied columns only;
- `recorded` defaults on insert and is never rewritten by a later import, so a re-import cannot
  move it. A source that carries its own `recorded` keeps that value.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

# The prototype schema, verbatim (issue #53, comment 6113435186). Do not reformat: the tests
# compare the live database against this text.
DDL = """
CREATE TABLE innies(
  innie TEXT PRIMARY KEY, outie TEXT, slack TEXT, github TEXT,
  host TEXT, backend TEXT, hardware TEXT, limits TEXT, notes TEXT, updated TEXT);
CREATE TABLE jobs(
  id INTEGER PRIMARY KEY, project TEXT DEFAULT 'fridica-research', ref TEXT,
  role TEXT, nature TEXT, size TEXT, innie TEXT,
  host TEXT, load_note TEXT,
  input_at TEXT, est_hours REAL, eta TEXT,
  est_basis TEXT, est_rationale TEXT, estimator TEXT,
  delivered_at TEXT, actual_hours REAL, error_hours REAL,
  delay_causes TEXT, notes TEXT, source TEXT, recorded TEXT DEFAULT (datetime('now')));
CREATE TABLE lessons(id INTEGER PRIMARY KEY, learned TEXT, context TEXT, lesson TEXT, rule TEXT, evidence TEXT);
CREATE TABLE waves(
  wave TEXT PRIMARY KEY, project TEXT DEFAULT 'fridica-research', thread TEXT,
  opened TEXT, closed TEXT, floor_manager TEXT, deputy TEXT, crew TEXT,
  issues TEXT, prs TEXT, main_before TEXT, main_after TEXT, validation TEXT,
  jobs_scored INTEGER, mae_hours REAL, bias_hours REAL, late INTEGER, early INTEGER,
  rescheduled INTEGER, max_late_hours REAL, max_early_hours REAL,
  delays TEXT, lessons TEXT, notes TEXT, recorded TEXT DEFAULT (datetime('now')));
CREATE TABLE workload(
  id INTEGER PRIMARY KEY, innie TEXT, asked_at TEXT, reported_at TEXT,
  machines TEXT, slots INTEGER, in_progress TEXT, queued TEXT, limits TEXT,
  offline TEXT, source TEXT, recorded TEXT DEFAULT (datetime('now')));
"""

TABLES = ("innies", "jobs", "lessons", "waves", "workload")
KEYS = {"innies": "innie", "jobs": "id", "lessons": "id", "waves": "wave", "workload": "id"}
DEFAULTED = ("recorded",)  # columns whose default is a clock: set once, never rewritten by an import
SCHEMA = DDL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")  # same tables; re-opening a ledger is not an error


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open (and create if need be) a ledger. The caller chooses the path; this module has no default."""
    if path != ":memory:": Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


@dataclass(frozen=True)
class ImportResult:
    """What one import did. A second import of the same records is all `unchanged`."""
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0  # rows without a primary key: nothing to match them on, so nothing to make idempotent

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.updated)

    def __add__(self, other: ImportResult) -> ImportResult:
        return ImportResult(self.inserted + other.inserted, self.updated + other.updated, self.unchanged + other.unchanged, self.skipped + other.skipped)


def import_rows(db: sqlite3.Connection, table: str, rows) -> ImportResult:
    """Carry `rows` (dicts) into `table`, matching on its primary key. Idempotent: see the module docstring."""
    if table not in TABLES: raise ValueError(f"unknown ledger table {table!r}")
    key, known = KEYS[table], columns(db, table)
    result = ImportResult()
    with db:
        for raw in rows:
            row = {c: raw[c] for c in known if c in raw}
            if unknown := set(raw) - set(known): raise ValueError(f"{table}: unknown column(s) {sorted(unknown)}")
            if row.get(key) is None: result += ImportResult(skipped=1); continue
            old = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (row[key],)).fetchone()
            if old is None:
                db.execute(f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
                result += ImportResult(inserted=1)
                continue
            # `recorded` (and any other clock default) is the row's own arrival stamp, not news from
            # the source: re-importing must not move it, so it is compared and written only if given.
            diff = {c: v for c, v in row.items() if c != key and old[c] != v}
            if not diff: result += ImportResult(unchanged=1); continue
            db.execute(f"UPDATE {table} SET {','.join(f'{c}=?' for c in diff)} WHERE {key}=?", (*diff.values(), row[key]))
            result += ImportResult(updated=1)
    return result


def import_records(db: sqlite3.Connection, records: dict) -> dict[str, ImportResult]:
    """Import several tables at once, parents before children (`innies` before `jobs`)."""
    return {t: import_rows(db, t, records[t]) for t in TABLES if t in records}


def import_sqlite(db: sqlite3.Connection, source: str | Path, tables=TABLES) -> dict[str, ImportResult]:
    """Carry a prototype ledger over, reading whichever of the five tables it has.

    This is how the floor manager's local `jobs.sqlite3` is read (#53). The path is the caller's;
    a table the source lacks is passed over rather than being an error, so an older prototype
    (no `waves`, no `workload`) imports cleanly.
    """
    src = sqlite3.connect(f"file:{Path(source)}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: import_rows(db, t, [dict(r) for r in src.execute(f"SELECT * FROM {t} ORDER BY {KEYS[t]}")]) for t in tables if t in present}
    finally:
        src.close()


def dump(db: sqlite3.Connection, tables=TABLES) -> dict[str, list[dict]]:
    """Every row of every table, ordered by primary key: the comparison an idempotency check needs."""
    return {t: [dict(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY {KEYS[t]}")] for t in tables}
