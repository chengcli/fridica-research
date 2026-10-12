"""#53 PR 1: the job ledger and its idempotent importer.

The schema under test is `ledger.DDL`: the prototype DDL from issue #53 (comment 6113435186,
2026-10-11T20:31:57Z) plus the four usage columns the R50 amendment (#54) adds to `workload`
after `offline`, so it is no longer the comment's text verbatim. A fresh ledger has the columns
in DDL order; a ledger file created before the amendment gains them on `connect`, appended after
`recorded` (SQLite's `ADD COLUMN` cannot insert). Every row here is synthetic: the floor
manager's real `jobs.sqlite3` stays local and is never read by a test.
"""
import re
import sqlite3

import pytest

from fridica_research import ledger

# -- the synthetic fixture ------------------------------------------------------
RECORDS = {
    "innies": [
        dict(innie="alfa", outie="A", slack="U0A", github="alfa-gh", host="h1", backend="claude", hardware="cpu", limits="1 slot", notes="", updated="2026-10-11T12:00:00Z"),
        dict(innie="bravo", outie="B", slack="U0B", github="bravo-gh", host="h2", backend="codex", hardware="gpu", limits="rate", notes="overnight", updated="2026-10-11T12:00:00Z"),
    ],
    "jobs": [
        dict(id=1, ref="#45", role="implementer", nature="pr", size="m", innie="alfa", host="h1", load_note="idle",
             input_at="2026-10-11T17:48:00Z", est_hours=2.0, eta="2026-10-11T19:48:00Z", est_basis="history",
             est_rationale="2.0 h work from history + no buffer", estimator="floor-manager",
             delivered_at="2026-10-11T18:09:00Z", actual_hours=0.35, error_hours=-1.65, delay_causes="", notes="", source="wave-a", recorded="2026-10-11T19:00:00Z"),
        dict(id=2, ref="#47", role="auditor", nature="review", size="s", innie="bravo", host="h2", load_note="busy",
             input_at="2026-10-11T18:00:00Z", est_hours=1.0, eta="2026-10-11T19:00:00Z", est_basis="trial",
             est_rationale="1.0 h work from trial + no buffer", estimator="floor-manager",
             delivered_at="2026-10-11T19:16:00Z", actual_hours=1.27, error_hours=0.27, delay_causes="sign-off queue", notes="", source="wave-a", recorded="2026-10-11T19:30:00Z"),
    ],
    "lessons": [dict(id=1, learned="2026-10-11", context="Wave A", lesson="rank a fresh workload report above history", rule="R49", evidence="#55, #56 landed early")],
    "waves": [dict(wave="A", thread="1791740688.583749", opened="2026-10-11T17:48:00Z", closed="2026-10-11T19:47:00Z",
                   floor_manager="cheng", deputy="deputy", crew="alfa,bravo", issues="#45,#47", prs="#55,#57",
                   main_before="d9f8cfb", main_after="ea450e1", validation="395 passed, 15/15 D0, ruff clean",
                   jobs_scored=8, mae_hours=0.81, bias_hours=-0.74, late=1, early=7, rescheduled=2,
                   max_late_hours=0.27, max_early_hours=-1.88, delays="silent Innie", lessons="see lessons.1", notes="", recorded="2026-10-11T20:00:00Z")],
    "workload": [dict(id=1, innie="alfa", asked_at="2026-10-11T17:40:00Z", reported_at="2026-10-11T17:44:00Z",
                      machines="h1", slots=1, in_progress="", queued="", limits="", offline="", source="slack", recorded="2026-10-11T17:45:00Z")],
}


@pytest.fixture
def db():
    conn = ledger.connect()
    yield conn
    conn.close()


def ddl_columns(table: str) -> list[str]:
    """The column names the issue's DDL text declares for `table`, in order."""
    body = re.search(rf"CREATE TABLE {table}\((.*?)\);", ledger.DDL, re.S).group(1)
    body = re.sub(r"\([^()]*\)", "", body)  # drop `DEFAULT (datetime('now'))` so its comma is not a separator
    return [part.split()[0] for part in body.split(",")]


# -- the schema matches the issue's DDL ----------------------------------------
def test_the_five_tables_are_created(db):
    assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == set(ledger.TABLES)
    assert set(ledger.TABLES) == {"innies", "jobs", "workload", "lessons", "waves"}


@pytest.mark.parametrize("table", ledger.TABLES)
def test_every_column_of_the_issue_ddl_is_present_in_order(db, table):
    assert ledger.columns(db, table) == ddl_columns(table)


WORKLOAD_BEFORE_R50 = ["id", "innie", "asked_at", "reported_at", "machines", "slots", "in_progress", "queued", "limits", "offline", "source", "recorded"]
USAGE = ["session_usage", "session_reset", "weekly_usage", "weekly_reset"]
INTAKE = ["free_slots", "total_slots", "client"]  # #84


def test_a_fresh_ledger_has_the_workload_columns_in_ddl_order(db):
    assert ledger.columns(db, "workload") == [*WORKLOAD_BEFORE_R50[:10], *USAGE, *INTAKE, "source", "recorded"]
    assert [c for c, _ in ledger.USAGE_COLUMNS] == USAGE


def test_a_migrated_ledger_appends_the_usage_columns_after_recorded(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(str(path), isolation_level=None)
    old.executescript(re.sub(r"offline TEXT, session_usage REAL, session_reset TEXT, weekly_usage REAL, weekly_reset TEXT,\n  free_slots INTEGER, total_slots INTEGER, client TEXT,", "offline TEXT,", ledger.DDL))
    old.close()
    db = ledger.connect(path)
    assert ledger.columns(db, "workload") == [*WORKLOAD_BEFORE_R50, *USAGE, *INTAKE]
    assert {t: ledger.columns(db, t) for t in ledger.TABLES if t != "workload"} == {t: ddl_columns(t) for t in ledger.TABLES if t != "workload"}
    db.close()
    again = ledger.connect(path)  # a second connect migrates nothing more
    assert ledger.columns(again, "workload") == [*WORKLOAD_BEFORE_R50, *USAGE, *INTAKE]
    again.close()


@pytest.mark.parametrize("table,key", ledger.KEYS.items())
def test_the_primary_key_is_the_one_the_ddl_declares(db, table, key):
    assert [r[1] for r in db.execute(f"PRAGMA table_info({table})") if r[5]] == [key]


def test_the_defaults_the_ddl_declares_are_live(db):
    db.execute("INSERT INTO jobs(id) VALUES(7)")
    row = db.execute("SELECT project, recorded FROM jobs WHERE id=7").fetchone()
    assert row["project"] == "fridica-research" and row["recorded"]
    db.execute("INSERT INTO waves(wave) VALUES('Z')")
    assert db.execute("SELECT project FROM waves WHERE wave='Z'").fetchone()[0] == "fridica-research"


def test_error_hours_is_delivered_minus_eta_so_early_is_negative(db):
    ledger.import_records(db, RECORDS)
    assert db.execute("SELECT error_hours FROM jobs WHERE ref='#45'").fetchone()[0] < 0  # early
    assert db.execute("SELECT error_hours FROM jobs WHERE ref='#47'").fetchone()[0] > 0  # late


def test_reopening_a_ledger_file_keeps_its_rows(tmp_path):
    path = tmp_path / "nested" / "jobs.sqlite3"
    first = ledger.connect(path)
    ledger.import_records(first, RECORDS)
    first.close()
    again = ledger.connect(path)
    assert len(ledger.dump(again)["jobs"]) == 2
    again.close()


# -- the importer ---------------------------------------------------------------
def test_import_carries_every_table_over(db):
    result = ledger.import_records(db, RECORDS)
    assert {t: r.inserted for t, r in result.items()} == {"innies": 2, "jobs": 2, "lessons": 1, "waves": 1, "workload": 1}
    dumped = ledger.dump(db)
    assert [r["ref"] for r in dumped["jobs"]] == ["#45", "#47"]
    assert dumped["waves"][0]["mae_hours"] == pytest.approx(0.81)
    assert dumped["innies"][0]["innie"] == "alfa"


def test_importing_the_same_records_twice_leaves_the_tables_unchanged(db):
    """The idempotency the issue asks for, checked on the whole table contents, not on a count."""
    ledger.import_records(db, RECORDS)
    before = ledger.dump(db)
    second = ledger.import_records(db, RECORDS)
    assert ledger.dump(db) == before  # every column of every row, `recorded` included
    assert all(not r.changed for r in second.values())
    assert {t: (r.inserted, r.updated, r.unchanged) for t, r in second.items()} == {
        "innies": (0, 0, 2), "jobs": (0, 0, 2), "lessons": (0, 0, 1), "waves": (0, 0, 1), "workload": (0, 0, 1)}
    third = ledger.import_records(db, RECORDS)
    assert ledger.dump(db) == before and not any(r.changed for r in third.values())


def test_a_reimport_does_not_move_the_recorded_stamp(db):
    """`recorded` defaults to a clock; if an import rewrote it, a re-import would never be a no-op."""
    rows = [{k: v for k, v in RECORDS["jobs"][0].items() if k != "recorded"}]
    ledger.import_rows(db, "jobs", rows)
    stamped = db.execute("SELECT recorded FROM jobs WHERE id=1").fetchone()[0]
    assert stamped
    assert ledger.import_rows(db, "jobs", rows).unchanged == 1
    assert db.execute("SELECT recorded FROM jobs WHERE id=1").fetchone()[0] == stamped


def test_a_growing_prototype_imports_only_its_new_and_changed_rows(db):
    ledger.import_records(db, RECORDS)
    grown = [*RECORDS["jobs"], dict(id=3, ref="#49", innie="alfa", est_hours=4.0, source="wave-b")]
    grown[1] = {**grown[1], "delay_causes": "sign-off queue; reviewer offline"}
    result = ledger.import_rows(db, "jobs", grown)
    assert (result.inserted, result.updated, result.unchanged) == (1, 1, 1)
    assert db.execute("SELECT delay_causes FROM jobs WHERE id=2").fetchone()[0] == "sign-off queue; reviewer offline"
    assert ledger.import_rows(db, "jobs", grown).unchanged == 3  # and it settles again


def test_a_partial_row_updates_only_the_columns_it_carries(db):
    ledger.import_records(db, RECORDS)
    ledger.import_rows(db, "jobs", [dict(id=1, notes="rescheduled once")])
    row = db.execute("SELECT * FROM jobs WHERE id=1").fetchone()
    assert row["notes"] == "rescheduled once" and row["ref"] == "#45" and row["est_basis"] == "history"


def test_a_row_without_a_primary_key_is_skipped_not_guessed(db):
    result = ledger.import_rows(db, "jobs", [dict(ref="#99", innie="alfa"), dict(id=None, ref="#98")])
    assert (result.skipped, result.inserted) == (2, 0) and ledger.dump(db)["jobs"] == []


def test_an_unknown_column_is_refused_rather_than_dropped_silently(db):
    with pytest.raises(ValueError, match="unknown column"):
        ledger.import_rows(db, "jobs", [dict(id=9, ref="#9", velocity=3)])


def test_an_unknown_table_is_refused(db):
    with pytest.raises(ValueError, match="unknown ledger table"):
        ledger.import_rows(db, "revisions", [])


# -- reading a prototype ledger file -------------------------------------------
def prototype(path):
    """A synthetic stand-in for the floor manager's local prototype, built from the same DDL."""
    src = sqlite3.connect(str(path), isolation_level=None)
    src.executescript(ledger.DDL)
    for table, rows in RECORDS.items():
        for row in rows:
            src.execute(f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
    src.close()
    return path


def test_import_sqlite_reads_a_prototype_and_is_idempotent(db, tmp_path):
    source = prototype(tmp_path / "jobs.sqlite3")
    first = ledger.import_sqlite(db, source)
    assert sum(r.inserted for r in first.values()) == 7
    before = ledger.dump(db)
    second = ledger.import_sqlite(db, source)
    assert ledger.dump(db) == before and not any(r.changed for r in second.values())


def test_import_sqlite_passes_over_a_table_an_older_prototype_lacks(db, tmp_path):
    path = tmp_path / "old.sqlite3"
    src = sqlite3.connect(str(path), isolation_level=None)
    src.executescript(re.sub(r"CREATE TABLE (waves|workload)\(.*?\);", "", ledger.DDL, flags=re.S))
    src.execute("INSERT INTO jobs(id, ref) VALUES(1, '#45')")
    src.close()
    result = ledger.import_sqlite(db, path)
    assert set(result) == {"innies", "jobs", "lessons"} and result["jobs"].inserted == 1


def test_the_source_ledger_is_opened_read_only(db, tmp_path):
    """The prototype is the floor manager's: an import reads it and never writes to it."""
    source = prototype(tmp_path / "jobs.sqlite3")
    before = source.read_bytes()
    ledger.import_sqlite(db, source)
    assert source.read_bytes() == before
