"""R49/R52: the job ledger (#53) — `innies`, `jobs`, `workload`, `lessons`, `waves`.

One SQLite file holding one row per job, who did it, what the ETA rested on and how it came out,
so the floor manager's ETAs can be scored (R48) and a wave can be closed with a record (R52).

`DDL` below is the prototype schema, copied verbatim from Cheng's comment on issue #53
(comment id 6113435186, 2026-10-11T20:31:57Z), plus the four usage columns of the R50 amendment
(#54: `session_usage`, `session_reset`, `weekly_usage`, `weekly_reset` on `workload`, after
`offline`), and the three intake columns of #84 (`free_slots`, `total_slots`, `client` on
`workload`, after `weekly_reset`; `slots` keeps its prototype meaning and is not overloaded);
`tests/test_issue53_ledger.py` checks the database this module creates against that
text column by column. A ledger file written before either amendment is brought up to date on
`connect` (`ALTER TABLE ... ADD COLUMN`, appended after `recorded`), and a prototype without the
columns still imports. The real prototype
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import eta, workload

# The prototype schema, verbatim (issue #53, comment 6113435186) plus the R50 amendment's usage
# columns (#54) and the intake columns (#84) on `workload`. Do not reformat: the tests compare the live database against this text.
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
  offline TEXT, session_usage REAL, session_reset TEXT, weekly_usage REAL, weekly_reset TEXT,
  free_slots INTEGER, total_slots INTEGER, client TEXT,
  source TEXT, recorded TEXT DEFAULT (datetime('now')));
"""

TABLES = ("innies", "jobs", "lessons", "waves", "workload")
KEYS = {"innies": "innie", "jobs": "id", "lessons": "id", "waves": "wave", "workload": "id"}
DEFAULTED = ("recorded",)  # columns whose default is a clock: set once, never rewritten by an import
SCHEMA = DDL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")  # same tables; re-opening a ledger is not an error
USAGE_COLUMNS = (("session_usage", "REAL"), ("session_reset", "TEXT"), ("weekly_usage", "REAL"), ("weekly_reset", "TEXT"))  # R50 amendment (#54), on `workload`
INTAKE_COLUMNS = (("free_slots", "INTEGER"), ("total_slots", "INTEGER"), ("client", "TEXT"))  # workload intake (#84), on `workload`


def ro_uri(path: str | Path) -> str:
    """The read-only SQLite URI of a file: `Path.resolve().as_uri()` percent-encodes `#`, `?`, `%` and spaces, which a bare `file:<path>` would not."""
    return Path(path).resolve().as_uri() + "?mode=ro"


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open (and create if need be) a ledger. The caller chooses the path; this module has no default."""
    if path != ":memory:": Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    have = columns(db, "workload")
    for name, kind in (*USAGE_COLUMNS, *INTAKE_COLUMNS):  # a ledger created before the R50 amendment or #84 gains their columns in place
        if name not in have: db.execute(f"ALTER TABLE workload ADD COLUMN {name} {kind}")
    return db


def open_existing(path: str | Path) -> sqlite3.Connection:
    """Open a ledger that must already exist, read-only: a missing path is an error, never an empty new ledger."""
    if not Path(path).is_file(): raise FileNotFoundError(f"no ledger at {path}")
    db = sqlite3.connect(ro_uri(path), uri=True)
    db.row_factory = sqlite3.Row
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
    rows = list(rows)
    for raw in rows:  # #64 note 1: the whole batch is validated before its first write, so a bad row leaves nothing half-imported
        if unknown := set(raw) - set(known): raise ValueError(f"{table}: unknown column(s) {sorted(unknown)}")
    result = ImportResult()
    with db:
        for raw in rows:
            row = {c: raw[c] for c in known if c in raw}
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
    (no `waves`, no `workload`) imports cleanly, and so does one whose `workload` predates the
    usage columns: a row carries only the columns its source has.
    """
    src = sqlite3.connect(ro_uri(source), uri=True)
    src.row_factory = sqlite3.Row
    try:
        present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: import_rows(db, t, [dict(r) for r in src.execute(f"SELECT * FROM {t} ORDER BY {KEYS[t]}")]) for t in tables if t in present}
    finally:
        src.close()


def dump(db: sqlite3.Connection, tables=TABLES) -> dict[str, list[dict]]:
    """Every row of every table, ordered by primary key: the comparison an idempotency check needs."""
    return {t: [dict(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY {KEYS[t]}")] for t in tables}


# -- scorecards (#53 PR 2a) -----------------------------------------------------------
@dataclass(frozen=True)
class Scorecard:
    """One group's record: jobs, delivered, coverage (delivered over jobs), and the R48 score of its scored jobs."""
    jobs: int = 0
    delivered: int = 0
    coverage: float = 0.0
    mae: float = 0.0
    bias: float = 0.0
    late: int = 0
    early: int = 0


def _when(text) -> datetime | None:
    """A ledger time (ISO text) as an aware datetime, UTC when it carries no offset; None when absent or unreadable."""
    try: t = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00")) if text else None
    except ValueError: return None
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def card(rows, tolerance_hours: float = 0.0) -> Scorecard:
    """`eta.score` over the rows that have both `eta` (the first committed ETA) and `delivered_at`;
    the error is recomputed from those two, never read from `error_hours`."""
    rows = list(rows)
    delivered = [r for r in rows if r["delivered_at"]]
    scored = [eta.Scored(str(r["ref"] or r["id"]), e, d) for r in delivered if (e := _when(r["eta"])) and (d := _when(r["delivered_at"]))]
    s = eta.score(scored, tolerance_hours)
    return Scorecard(jobs=len(rows), delivered=len(delivered), coverage=len(delivered) / len(rows) if rows else 0.0,
                     mae=s.mae_hours, bias=s.bias_hours, late=s.late, early=s.early)


def scorecards(db: sqlite3.Connection, tolerance_hours: float = 0.0) -> dict:
    """Scorecards per Innie, per role and for the whole crew: `{"innie": {name: Scorecard}, "role": {...}, "crew": Scorecard}`."""
    rows = [dict(r) for r in db.execute("SELECT id, ref, role, innie, eta, delivered_at FROM jobs ORDER BY id")]
    def by(col): return {k: card([r for r in rows if (r[col] or "") == k], tolerance_hours) for k in sorted({r[col] or "" for r in rows})}
    return {"innie": by("innie"), "role": by("role"), "crew": card(rows, tolerance_hours)}


def scorecard_table(cards: dict) -> str:
    """The scorecards as one Markdown table: what `score` prints and what `score --board` writes to the project readme."""
    out = ["| Level | Name | Jobs | Delivered | Coverage | MAE (h) | Bias (h) | Late | Early |", "|---|---|---|---|---|---|---|---|---|"]
    def line(level, name, c): out.append(f"| {level} | {name or '(none)'} | {c.jobs} | {c.delivered} | {c.coverage:.0%} | {c.mae:.2f} | {c.bias:+.2f} | {c.late} | {c.early} |")
    for level in ("innie", "role"):
        for name, c in cards[level].items(): line(level, name, c)
    line("crew", "all", cards["crew"])
    return "\n".join(out)


# -- workload intake (#84) -------------------------------------------------------------
FRESH_HOURS = 6.0  # R50: a report is fresh this long after it was posted, or until a usage reset it carries passes


def ingest(db: sqlite3.Connection, report: dict, source: str) -> ImportResult:
    """Write one parsed report (`workload.parse(...).row()`) as a `workload` row.

    Idempotent on `(innie, source)`: the same post ingested again matches its earlier row and
    changes nothing; an edited post updates that row in place. `recorded` is set once."""
    if not report.get("innie") or not source: raise ValueError("ingest needs an innie and a source")
    row = {**report, "source": source}
    old = db.execute("SELECT id FROM workload WHERE innie=? AND source=? ORDER BY id LIMIT 1", (row["innie"], source)).fetchone()
    row["id"] = old["id"] if old else (db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM workload").fetchone()[0])
    return import_rows(db, "workload", [row])


def ingest_posts(db: sqlite3.Connection, posts, register, now: datetime, thread: str = "") -> tuple[ImportResult, list[dict]]:
    """`workload.intake` then `ingest` for each row: a thread's posts into `workload` rows through the register.
    Returns what the writes did and the intake's findings (dicts `{source, sender, reason}`)."""
    rows, findings = workload.intake(posts, register, now, thread)
    result = ImportResult()
    for row in rows: result += ingest(db, {k: v for k, v in row.items() if k != "source"}, row["source"])
    return result, findings


def working(register, innie: str, now: datetime) -> bool:
    """Whether `now` falls inside the Innie's registered working hours, read as ET (`workload.ET`, so EDT or EST
    as the date has it). An Innie the register does not list, or one without `hours`, counts as working."""
    entry = register.get(innie) if register is not None else None
    if entry is None: return True
    local = now.astimezone(workload.ET)
    return entry.in_window("hours", local.hour * 60 + local.minute) is not False


def fresh(row, now: datetime, hours: float = FRESH_HOURS) -> bool:
    """Whether a `workload` row still speaks for its Innie at `now`: posted no more than `hours`
    ago (and not in the future), and no usage reset it reports has passed since."""
    at = _when(row["reported_at"])
    if at is None or not at <= now <= at + timedelta(hours=hours): return False
    return all(r is None or now < r for r in (_when(row["session_reset"]), _when(row["weekly_reset"])))


def latest(db: sqlite3.Connection, now: datetime, hours: float = FRESH_HOURS) -> dict[str, dict]:
    """The newest fresh report per Innie at `now`; an Innie whose newest report is stale is absent."""
    out: dict[str, dict] = {}
    for r in db.execute("SELECT * FROM workload WHERE innie IS NOT NULL ORDER BY reported_at, id"):
        out[r["innie"]] = dict(r)
    return {k: r for k, r in out.items() if fresh(r, now, hours)}


def alternatives(db: sqlite3.Connection, now: datetime, hours: float = FRESH_HOURS, exclude=(), register=None) -> list[str]:
    """Innies with a fresh report and a free slot that are not capped, best first (R51's "first free alternative").

    Over quota (`session_usage >= eta.SESSION_QUOTA`) or capped (`weekly_usage >= eta.WEEKLY_CAP`)
    is out. Usage that is unknown (NULL, posted `unreadable`) is not capped, but such an Innie
    ranks after every Innie whose usage is known; so does one whose free slots are unknown.
    Ties go to more free slots, then lower weekly usage, then name. `exclude` drops Innies
    (the one already holding the job's other role, for example). With a `register`, an Innie
    outside its working hours at `now` (in ET) is out; see `working`."""
    ranked = []
    for name, r in latest(db, now, hours).items():
        if name in exclude or r["free_slots"] == 0 or not working(register, name, now): continue
        s, w = r["session_usage"], r["weekly_usage"]
        if (s is not None and s >= eta.SESSION_QUOTA) or (w is not None and w >= eta.WEEKLY_CAP): continue
        ranked.append(((s is None or w is None, r["free_slots"] is None, -(r["free_slots"] or 0), w if w is not None else 0.0, name), name))
    return [name for _, name in sorted(ranked)]


# -- #94: the row writers. Each is idempotent on its natural key and uses the tables above (no DDL change); times are UTC ISO.
RESCHED = " (resched)"


def _hours(a, b) -> float | None:
    s, e = _when(a), _when(b)
    return (e - s).total_seconds() / 3600.0 if s and e else None


def _job(db: sqlite3.Connection, source: str, ref: str, role: str):
    return db.execute("SELECT * FROM jobs WHERE source=? AND ref=? AND role=? ORDER BY id LIMIT 1", (source, ref, role)).fetchone()


def open_job(db: sqlite3.Connection, *, source: str, ref: str, role: str, innie: str, input_at: str, eta: str, est_hours,
             est_basis: str = "", nature: str = "", size: str = "") -> None:
    """One `jobs` row per (source, ref, role). Its `eta` is the first committed ETA (R52) and is never changed: a later
    call with another ETA only notes `re-estimate <iso>` in `notes`, once per ETA."""
    row = _job(db, source, ref, role)
    if row is None:
        db.execute("INSERT INTO jobs(source, ref, role, innie, input_at, eta, est_hours, est_basis, nature, size) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (source, ref, role, innie, input_at, eta, est_hours, est_basis, nature, size))
    elif eta and eta != row["eta"] and f"re-estimate {eta}" not in (row["notes"] or ""):
        db.execute("UPDATE jobs SET notes=? WHERE id=?", ("; ".join(x for x in (row["notes"], f"re-estimate {eta}") if x), row["id"]))
    db.commit()


def deliver(db: sqlite3.Connection, *, source: str, ref: str, role: str, delivered_at: str) -> None:
    """Set `delivered_at`, `actual_hours` (from `input_at`) and `error_hours` (delivered - eta) on the (source, ref, role) row."""
    row = _job(db, source, ref, role)
    if row is None: return
    db.execute("UPDATE jobs SET delivered_at=?, actual_hours=?, error_hours=? WHERE id=?",
               (delivered_at, _hours(row["input_at"], delivered_at), _hours(row["eta"], delivered_at), row["id"]))
    db.commit()


def reschedule(db: sqlite3.Connection, *, source: str, ref: str, role: str, to_innie: str, eta: str, at: str) -> None:
    """Record the cause on the original row (still scored against its first ETA when the job is delivered: the Wave C
    convention) and open a `<role> (resched)` row for the new holder, which the score leaves out."""
    row = _job(db, source, ref, role)
    cause = f"rescheduled {at} to {to_innie}"
    if row is not None and cause not in (row["delay_causes"] or ""):
        db.execute("UPDATE jobs SET delay_causes=? WHERE id=?", ("; ".join(x for x in (row["delay_causes"], cause) if x), row["id"]))
    open_job(db, source=source, ref=ref, role=role + RESCHED, innie=to_innie, input_at=at, eta=eta, est_hours=_hours(at, eta))


def write_wave(db: sqlite3.Connection, *, wave: str, source: str, opened: str, closed: str, crew: str, prs: str, main_before: str,
               main_after: str, validation: str, score) -> None:
    """INSERT OR REPLACE the `waves` row named `wave` (thread `source`); `score` is an `eta.Score` or a `Scorecard`."""
    if isinstance(score, Scorecard): n, mae, bias = score.delivered, score.mae, score.bias
    else: n, mae, bias = score.jobs_scored, score.mae_hours, score.bias_hours
    db.execute("INSERT OR REPLACE INTO waves(wave, thread, opened, closed, crew, prs, main_before, main_after, validation, jobs_scored, mae_hours,"
               " bias_hours, late, early, max_late_hours, max_early_hours) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (wave, source, opened, closed, crew, prs, main_before, main_after, validation, n, mae, bias, score.late, score.early,
                getattr(score, "max_late_hours", None), getattr(score, "max_early_hours", None)))
    db.commit()


def close_text_score(db: sqlite3.Connection, source: str) -> str:
    """The close post's score line over `source`'s original-role rows (`(resched)` rows are left out)."""
    c = card(db.execute("SELECT * FROM jobs WHERE source=? AND role NOT LIKE ? ORDER BY id", (source, "%" + RESCHED)).fetchall())
    return f"n={c.delivered}, MAE {c.mae:.2f}h, bias {c.bias:+.2f}h, late {c.late}, early {c.early}"
