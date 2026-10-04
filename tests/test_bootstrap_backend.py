"""R15: the bootstrap (feed-journal) backend. Swap-equivalence with the fake control server on corpus 000_bootstrap,
the journal's single writer, the own-post echo through the poll only, the 2x-projection kill, the cost ceiling,
redaction, deterministic sessions (restart resumes), the retry backoff, and the CLI start/serve flow."""
from __future__ import annotations

import dataclasses
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from fridica_research import cli, config, contracts, replay
from fridica_research.backend import build_backend
from fridica_research.backend.bootstrap import NAMESPACE, REDACT, action_key, BootstrapBackend, Journal, ProcessRunner, next_ts, process_start, run_key, worker_env
from fridica_research.backend.fridica import FridicaBackend
from fridica_research.backend.protocol import ControlError, Unavailable
from fridica_research.client import Client
from fridica_research.driver import Driver
from fridica_research.machine import Action
from fridica_research.store import Store
from fake_control import FakeControl
from support import CFG, EXPLORER_REPORT, LLM, OWNER, report, result
from test_bootstrap_tape import BOOTSTRAP, EXPLORE_REPORT, STAGE_MIN, THREAD

ROOT_TEXT = "Bootstrap: implement fridica-research (fridica #126)\n\nprojected: 4 h\ngeneration: 1\nlineage: origin\nref: start"
PR2 = "https://github.com/chengcli/fridica-research/pull/2"
ROLE_OF = {"explorer": "explorer", "mathematician": "mathematician", "physicist": "physicist", "impl": "implementer", "audit": "auditor"}


class Clock:
    def __init__(self, t=1_700_000_000.0): self.t = t
    def __call__(self): return self.t


def role_of(argv: list[str]) -> str:
    """The fake claude learns its role from the `ref:` line of the brief (the last argv entry)."""
    suffix = (contracts.ref_of(argv[-1]) or "").rsplit("/", 1)[-1]
    return next(v for k, v in ROLE_OF.items() if k in suffix)


def claude_json(payload: dict, argv: list[str], cost=0.01) -> str:
    sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "num_turns": 1, "total_cost_usd": cost, "structured_output": payload})


def payload(res: dict) -> dict:
    """A scripted WorkerResult as the worker would emit it (the backend fills machine_state from git)."""
    return {k: res[k] for k in ("status", "summary", "report", "artifacts", "unresolved")}


def bootstrap_cfg(tmp_path, base=CFG, **bs) -> config.Config:
    b = config.Bootstrap(**{"journal_dir": str(tmp_path / "journal"), "retry_backoff": 0.0, **bs})
    return dataclasses.replace(base, state_path=str(tmp_path / "state.sqlite3"), backend=config.Backend("bootstrap", b))


def make_driver(cfg, backend, clock=None, store=None):
    store = store or Store(cfg.state_file)
    if store.cursor is None: store.set_cursor(0)
    return Driver(cfg, backend, store, llm=lambda n, p: LLM[n], clock=clock or Clock(), sleep=lambda s: None)


def drain(drv: Driver, limit: int = 12):
    for _ in range(limit):
        n, at_end = drv.run_once()
        if n == 0 and at_end: return
    raise AssertionError("feed did not settle")


def recording(drv: Driver) -> list[dict]:
    out, orig = [], drv.execute
    def execute(state, a):
        out.append(a.to_dict())
        return orig(state, a)
    drv.execute = execute
    return out


def git_repo(path: Path) -> str:
    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "one"], check=True, env=env)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


# -- the scenario of corpus 000_bootstrap, driven through either backend --------------------------------
class Scripted:
    """One study through the driver; `finish`/`peer`/`advance` are the points where the two backends are fed the same thing."""

    def __init__(self, cfg, drv: Driver, clock: Clock):
        self.cfg, self.drv, self.clock = cfg, drv, clock
        self.actions = recording(drv)

    def advance(self, minutes: float): self.clock.t += minutes * 60
    def drain(self): drain(self.drv)


class ViaFake(Scripted):
    def __init__(self, cfg, sock, clock, machine_state):
        self.server = FakeControl(sock, owner=OWNER, workspace="TJ6E2EJ2K").start()
        cfg = dataclasses.replace(cfg, socket=sock)
        super().__init__(cfg, make_driver(cfg, FridicaBackend(Client(cfg.socket_path)), clock), clock)
        self.ms = machine_state

    def root(self): self.server.root(THREAD, OWNER, ROOT_TEXT)
    def finish(self, role, res): self.server.finish_job(THREAD, self.server.job_of(THREAD, role)["id"], {**res, "machine_state": self.ms, "changes": [], "validation": [], "question": None})
    def peer(self, sender, text): self.server.message(THREAD, sender, text)
    def close(self): self.server.stop()


class ViaJournal(Scripted):
    def __init__(self, cfg, clock):
        self.queues = {r: queue.Queue() for r in ROLE_OF.values()}
        self.calls: list[tuple[list[str], str, float]] = []
        self.heads: list[str] = []
        def runner(argv, cwd, timeout):
            self.calls.append((argv, cwd, timeout))
            self.heads.append(subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
            return claude_json(self.queues[role_of(argv)].get(), argv), 0  # blocks until the scenario releases this role's result
        self.backend = BootstrapBackend(cfg, runner=runner, clock=clock, sleep=lambda s: None)
        super().__init__(cfg, make_driver(cfg, self.backend, clock), clock)

    def root(self):
        ws, ch, ts = THREAD.split(":")
        self.backend.journal.append({"kind": "message", "workspace": ws, "channel": {"id": ch, "name": None}, "thread": ts, "ts": ts, "sender": OWNER, "meta": {"kind": "study_root", "status": "complete"}, "turn_kind": "study_root", "text": ROOT_TEXT})

    def finish(self, role, res):
        job = next(j for j in self.backend.jobs.values() if j["role"] == role and not j["stopped"])
        self.queues[role].put(payload(res))
        self.backend.threads[job["job_id"]].join(10)

    def peer(self, sender, text):
        ws, ch, ts = THREAD.split(":")
        self.backend.journal.append({"kind": "message", "workspace": ws, "channel": {"id": ch, "name": None}, "thread": ts, "sender": sender, "meta": None, "text": text}, now=self.clock())

    def close(self): pass


def kinds_and_ids(actions: list[dict]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for a in actions:
        pair = (a["kind"], a["id"])
        if pair[0] == "cancel_timer" and out and out[-1][0] == "cancel_timer":
            run = [x for x in out[len(out) - next(i for i, x in enumerate(reversed(out)) if x[0] != "cancel_timer"):]] if any(x[0] != "cancel_timer" for x in out) else out[:]
            del out[len(out) - len(run):]
            out.extend(sorted(run + [pair]))
        else: out.append(pair)
    return out


def scenario(p: Scripted, sha: str):
    """The tape of tests/test_bootstrap_tape.py, through the driver: the same clock advances and worker results on either backend."""
    p.root()
    p.drain()
    p.advance(STAGE_MIN["Explore"])
    p.finish("explorer", result(report=EXPLORE_REPORT, summary="three package shapes"))
    p.drain()
    assert p.drv.store.load(THREAD).claim["slug"] == "pure-machine-sqlite"
    p.advance(STAGE_MIN["Claim"])
    p.drain()  # the settle timer (60 s) fired: Debate round 1
    assert p.drv.store.load(THREAD).stage == "Debate"
    p.finish("mathematician", result(report=report(position="revised", body="state space, transition table, invariants")))
    p.finish("physicist", result(report=report(position="revised", body="scales, balances, five tests")))
    p.drain()
    p.advance(STAGE_MIN["Debate"])
    p.finish("mathematician", result(report=report(position="revised", body="concede snapshot store; keep waiting ledger")))
    p.finish("physicist", result(report=report(position="revised", body="concede snapshot; hold no log")))
    p.drain()
    assert p.drv.store.load(THREAD).stage == "Implement"
    p.advance(STAGE_MIN["Implement"])
    p.finish("implementer", result(status="done", artifacts=[PR2]))
    p.drain()
    s = p.drv.store.load(THREAD)
    assert s.stage == "Audit" and s.phase == "signoff" and s.implementer["sha"] == sha
    p.advance(20)
    p.peer("U_A", f"SIGN-OFF {PR2} {sha} approve")
    p.drain()
    p.advance(STAGE_MIN["Audit"] - 20)
    p.peer("U_B", f"SIGN-OFF {PR2} {sha} approve")
    p.peer("U_C", f"SIGN-OFF {PR2} {sha} approve")
    p.drain()
    s = p.drv.store.load(THREAD)
    assert s.stage == "Delivered" and s.followon and p.drv.store.load(s.followon).stage == "Explore"
    return [a for a in p.actions if a["id"].startswith(THREAD + "/")]


def test_swap_equivalence_on_corpus_000_bootstrap(tmp_path, sock_dir):
    sha = git_repo(tmp_path / "subject")
    cfg = dataclasses.replace(BOOTSTRAP, board=config.Board(), state_path=str(tmp_path / "a.sqlite3"))
    a = ViaFake(cfg, os.path.join(sock_dir, "c.sock"), Clock(1791125606.0), {"branch": "HEAD", "commit": sha, "dirty": False})
    try: via_fake = scenario(a, sha)
    finally: a.close()
    bcfg = bootstrap_cfg(tmp_path, cfg, subject_repo=str(tmp_path / "subject"), workspace="TJ6E2EJ2K")
    b = ViaJournal(bcfg, Clock(1791125606.0))
    via_journal = scenario(b, sha)
    assert [replay.pi_struct(x) for x in via_journal] == [replay.pi_struct(x) for x in via_fake]
    assert via_journal == via_fake  # byte-equal as well: same ids, same worker ids, same texts
    # The corpus recorded from `World` (tests/record_corpora.py) has the same action kinds and ids as either driver path, up to the
    # order of simultaneous `cancel_timer`s (the driver's snapshot round-trips through sort_keys JSON, so `cancel_all` runs in key order).
    expected = [json.loads(line) for line in (replay.corpora(Path(__file__).parent / "bootstrap" / "000_bootstrap")[0] / "expected_actions.jsonl").read_text().splitlines()]
    assert kinds_and_ids(via_journal) == kinds_and_ids(expected)
    # Every worker ran from a detached worktree of the subject revision, one per worker id, reused across rounds (the 7th call is the follow-on child's explorer).
    cwds = {c[1] for c in b.calls}
    assert len(b.calls) == 7 and len(cwds) == 5 and len({c[1] for c in b.calls[:6]}) == 4 and all(Path(c).parent == Path(bcfg.backend.bootstrap.journal_dir) / "worktrees" for c in cwds)
    deadline = time.monotonic() + 10
    while len(b.heads) < 7 and time.monotonic() < deadline: time.sleep(0.01)  # the child's explorer thread records its head after its call
    assert b.heads == [sha] * 7
    # The parent reached Delivered: its four workers' worktrees are gone (`git worktree remove`); the child's explorer keeps its own.
    listed = subprocess.run(["git", "-C", str(tmp_path / "subject"), "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
    parent, child = {c[1] for c in b.calls[:6]}, b.calls[6][1]
    assert not any(Path(c).exists() or c in listed for c in parent) and Path(child).exists() and child in listed
    # The echo of every own post arrived through the poll with a cursor and a fixed-width ts, never synthesised in execute.
    msgs = [r for r in b.backend.journal.all() if r["kind"] == "message" and r["sender"] == OWNER]
    assert all(r["cursor"] == r["seq"] and len(r["ts"].split(".")[1]) == 6 for r in msgs)
    assert [m["meta"]["kind"] for m in msgs] == ["study_root", "study_claim", "report", "study_result", "study_root"]
    jr = [r for r in b.backend.journal.all() if r["kind"] == "job_result"]
    assert [r["role"] for r in jr] == ["explorer", "mathematician", "physicist", "mathematician", "physicist", "implementer"] and all(r["total_cost_usd"] == 0.01 for r in jr)
    assert b.backend.spent() == 0.06


# -- journal invariants ----------------------------------------------------------------------------------
WRITER = """
import sys, json
from fridica_research.backend.bootstrap import Journal
j = Journal(sys.argv[1])
for i in range(int(sys.argv[3])): j.append({"kind": "message", "writer": sys.argv[2], "i": i, "text": sys.argv[2] * 6000}, now=1.0)
"""


def test_journal_single_writer_two_processes_never_interleave(tmp_path):
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(tmp_path), w, "120"]) for w in ("a", "b")]
    assert [p.wait(120) for p in procs] == [0, 0]
    rows = Journal(tmp_path).all()  # a seq gap or an unparseable line raises
    assert [r["seq"] for r in rows] == list(range(1, 241)) and all(r["text"] == r["writer"] * 6000 for r in rows)
    assert sorted(r["writer"] for r in rows).count("a") == 120


def test_journal_reader_stops_on_a_seq_gap(tmp_path):
    j = Journal(tmp_path)
    j.append({"kind": "x"})
    j.append({"kind": "x"})
    rows = j.path.read_text().splitlines()
    j.path.write_text(rows[0] + "\n" + rows[1].replace('"seq": 2', '"seq": 3') + "\n")
    with pytest.raises(Unavailable): j.all()


def test_ts_is_fixed_width_and_strictly_increasing():
    assert next_ts(None, 1700.9) == "1700.000000"
    assert next_ts("1700.000000", 1700.0) == "1700.000001"
    assert next_ts("1700.999999", 1700.0) == "1701.000000"
    assert next_ts("1700.000005", 1650.0) == "1700.000006"  # the clock moved back: still after the last line
    assert next_ts("1700.000005", 1800.0) == "1800.000000"


def test_own_post_echo_arrives_only_through_events(tmp_path):
    cfg = bootstrap_cfg(tmp_path)
    def running_forever(argv, cwd, timeout):  # the explorer stays running for the whole test
        threading.Event().wait(10)
        return "", None
    backend = BootstrapBackend(cfg, runner=running_forever, sleep=lambda s: None)
    drv = make_driver(cfg, backend)
    thread = backend.post("C1", contracts.PostRequest("study_root", "Study\n\nprojected: 1 h\ngeneration: 1\nlineage: origin\nref: r"))["thread"]
    drain(drv)
    state = drv.store.load(thread)
    assert state.stage == "Explore"
    cursor = drv.store.cursor
    assert drv.execute(state, Action("post", "x/claim", {"thread": thread, "post_kind": "study_claim", "text": "Claim\nref: x/claim", "details": None})) == []  # no synthesised follow event
    page = backend.events(cursor)["events"]
    assert len(page) == 1 and page[0]["cursor"] == cursor + 1 and page[0]["text"] == "Claim\nref: x/claim" and page[0]["meta"]["kind"] == "study_claim" and page[0]["sender"] == OWNER
    assert drv.thread_view(thread).own_post_with_ref("x/claim", OWNER)["ts"] == page[0]["ts"]


# -- bounds: the 2x-projection kill, the cost ceiling, the backoff --------------------------------------
def test_kill_at_twice_the_stage_projection_through_the_driver(tmp_path):
    cfg = bootstrap_cfg(tmp_path, dataclasses.replace(CFG, projection={**CFG.projection, "explore": 1}))
    seen = []
    def sleeper(argv, cwd, timeout):
        seen.append(timeout)
        time.sleep(min(timeout, 5))
        return "", None  # the ProcessRunner's answer after os.killpg at the deadline
    backend = BootstrapBackend(cfg, runner=sleeper, sleep=lambda s: None)
    drv = make_driver(cfg, backend)
    thread = backend.post("C1", contracts.PostRequest("study_root", "Study\n\nprojected: 1 h\ngeneration: 1\nlineage: origin\nref: r"))["thread"]
    drain(drv)
    backend.join(10)
    drain(drv)
    assert seen[0] == 2.0  # OVERRUN_FACTOR x projection["explore"]
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert jr[0]["job_status"] == "interrupted" and jr[0]["code"] == "timeout" and jr[0]["result"] is None
    s = drv.store.load(thread)
    assert s.attempt == 2 and s.stage == "Explore"  # rule R re-ran the stage (a new ActionId, attempt 1 of it: no backoff)
    assert len(seen) == 2 and len(list((Path(cfg.backend.bootstrap.journal_dir) / "results").glob("*.json"))) == 1  # the killed job's result file (the retry is still running)


def test_process_runner_kills_the_process_group_at_the_deadline(tmp_path):
    t0 = time.monotonic()
    out, rc = ProcessRunner()(["sh", "-c", "sleep 30 & sleep 30"], str(tmp_path), 0.3)
    assert rc is None and time.monotonic() - t0 < 5


def test_process_runner_returns_stdout_and_exit_code(tmp_path):
    assert ProcessRunner()(["sh", "-c", "echo hi; exit 3"], str(tmp_path), 5) == ("hi\n", 3)


def test_cost_ceiling_refuses_a_delegate(tmp_path):
    cfg = bootstrap_cfg(tmp_path, max_budget_usd_per_job=5.0, max_cost_usd_per_study=4.0)
    backend = BootstrapBackend(cfg, runner=lambda a, c, t: ("", 0), sleep=lambda s: None)
    with pytest.raises(ControlError) as e: backend.delegate("journal:C1:1.000000", contracts.DelegateRequest("explorer", "b\nref: t/g1/i1/Explore/a1/explorer", tags=("t/g1/i1/Explore/a1/explorer",)))
    assert (e.value.status, e.value.code) == (429, "budget_exceeded")
    # Through the driver: the refusal is rule R, the second refusal Blocks the study.
    drv = make_driver(cfg, backend)
    thread = backend.post("C1", contracts.PostRequest("study_root", "Study\n\nprojected: 1 h\ngeneration: 1\nlineage: origin\nref: r"))["thread"]
    drain(drv)
    s = drv.store.load(thread)
    assert s.stage == "Blocked" and s.attempt == 2 and not [r for r in backend.journal.all() if r["kind"] == "job"]


def test_cost_accumulates_from_total_cost_usd(tmp_path):
    cfg = bootstrap_cfg(tmp_path, max_budget_usd_per_job=1.0, max_cost_usd_per_study=2.5)
    def runner(argv, cwd, timeout): return claude_json(payload(result(report="r")), argv, cost=1.25), 0
    backend = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)
    thread = "journal:C1:1.000000"
    for i in range(2):
        backend.delegate(thread, contracts.DelegateRequest("explorer", f"b\nref: t/g1/i1/Explore/a{i}/explorer", tags=(f"t/g1/i1/Explore/a{i}/explorer",)))
        backend.join(10)
        backend.events(0)
    assert backend.spent() == 2.5
    with pytest.raises(ControlError): backend.delegate(thread, contracts.DelegateRequest("explorer", "b\nref: t/g1/i1/Explore/a3/explorer", tags=("t/g1/i1/Explore/a3/explorer",)))


def test_retry_of_a_seen_action_id_backs_off_exponentially(tmp_path):
    cfg = bootstrap_cfg(tmp_path, retry_backoff=30.0)
    slept = []
    backend = BootstrapBackend(cfg, runner=lambda a, c, t: ("", 1), sleep=slept.append)
    aid = "t/g1/i1/Explore/a1/explorer"
    for _ in range(4):
        backend.delegate("journal:C1:1.000000", contracts.DelegateRequest("explorer", f"b\nref: {aid}", tags=(aid,)))
        backend.join(10)
    assert slept == [30.0, 60.0, 120.0]
    assert [r["attempt"] for r in backend.journal.all() if r["kind"] == "job"] == [1, 2, 3, 4]


# -- redaction, sessions, restart -----------------------------------------------------------------------
def test_everything_written_to_the_journal_is_redacted(tmp_path):
    cfg = bootstrap_cfg(tmp_path)
    def runner(argv, cwd, timeout): return claude_json(payload(result(report="token xoxb-1234567890 and Bearer abcdef0123")), argv), 0
    backend = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)
    thread = backend.post("C1", contracts.PostRequest("study_root", "root sk-abcdefghijkl\n\nref: r", details="ghp_ABCDEFGH1234"))["thread"]
    backend.delegate(thread, contracts.DelegateRequest("explorer", "brief with xoxp-999999999\nref: t/g1/i1/Explore/a1/explorer", tags=("t/g1/i1/Explore/a1/explorer",)))
    backend.join(10)
    page = backend.events(0)["events"]
    assert page[0]["text"] == "root [redacted]\n\nref: r" and page[0]["details"] == "[redacted]"
    assert page[1]["brief"] == "brief with [redacted]\nref: t/g1/i1/Explore/a1/explorer"
    assert page[2]["result"]["report"] == "token [redacted] and [redacted]"
    assert not replay.TOKEN.search(backend.journal.path.read_text())
    assert not any(replay.TOKEN.search(f.read_text()) for f in (Path(cfg.backend.bootstrap.journal_dir) / "results").iterdir())


def test_restart_resumes_the_same_worker_session(tmp_path):
    cfg = bootstrap_cfg(tmp_path)
    calls = []
    def runner(argv, cwd, timeout):
        calls.append(argv)
        return claude_json(payload(result(report="r")), argv), 0
    aid = "t/g1/i1/Debate/a1/debate-1-mathematician"
    b1 = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)
    r = b1.delegate("journal:C1:1.000000", contracts.DelegateRequest("mathematician", f"b\nref: {aid}", tags=(aid,)))
    b1.join(10)
    sid = str(uuid.uuid5(NAMESPACE, aid))
    assert calls[0][calls[0].index("--session-id") + 1] == sid and "--resume" not in calls[0]
    b2 = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)  # a restarted driver: the restart probe re-sends the same ActionId
    b2.delegate("journal:C1:1.000000", contracts.DelegateRequest("mathematician", f"b\nref: {aid}", tags=(aid,)))
    b2.join(10)
    assert calls[1][calls[1].index("--resume") + 1] == sid and "--session-id" not in calls[1]
    # A later job for the same persistent worker (round 2) resumes that worker's session too.
    aid2 = "t/g1/i1/Debate/a1/debate-2-mathematician"
    b2.delegate("journal:C1:1.000000", contracts.DelegateRequest("mathematician", f"b\nref: {aid2}", worker_id=r["jobs"][0]["worker_id"], tags=(aid2,)))
    b2.join(10)
    assert calls[2][calls[2].index("--resume") + 1] == sid
    argv = calls[0]
    assert argv[:4] == ["claude", "-p", "--output-format", "json"] and "--json-schema" in argv and argv[argv.index("--max-budget-usd") + 1] == "5" and argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_restart_adopts_a_running_job_and_applies_a_result_file(tmp_path):
    """The result file keyed by ActionId is durable: a driver restarted after the worker finished finds the job done through the thread view."""
    cfg = bootstrap_cfg(tmp_path)
    gate = threading.Event()
    def runner(argv, cwd, timeout):
        gate.wait(10)
        return claude_json(payload(result(report=EXPLORER_REPORT)), argv), 0
    backend = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)
    drv = make_driver(cfg, backend)
    thread = backend.post("C1", contracts.PostRequest("study_root", "Study\n\nprojected: 1 h\ngeneration: 1\nlineage: origin\nref: r"))["thread"]
    drain(drv)
    s = drv.store.load(thread)
    assert s.stage == "Explore" and drv.thread_view(thread).jobs_with_ref(s.waiting["actions"][0]["action_id"])[0]["job_status"] == "running"
    gate.set()
    backend.join(10)  # the worker finished and its result file was written, but the driver died before the journal line
    drv2 = make_driver(cfg, BootstrapBackend(cfg, runner=runner, sleep=lambda s: None), store=Store(cfg.state_file))
    drain(drv2)
    s2 = drv2.store.load(thread)
    assert s2.stage == "Claim" and len([r for r in drv2.backend.journal.all() if r["kind"] == "job"]) == 1  # adopted and finished, never re-delegated


def test_stop_interrupts_the_worker(tmp_path):
    cfg = bootstrap_cfg(tmp_path)
    killed = []
    class R:
        def __call__(self, argv, cwd, timeout):
            time.sleep(0.2)
            return "", None
        def kill(self, argv): killed.append(argv[0])
    backend = BootstrapBackend(cfg, runner=R(), sleep=lambda s: None)
    drv = make_driver(cfg, backend)
    thread = backend.post("C1", contracts.PostRequest("study_root", "Study\n\nprojected: 1 h\ngeneration: 1\nlineage: origin\nref: r"))["thread"]
    drain(drv)
    drv.store.command(thread, "stop")
    drain(drv)
    backend.join(10)
    drain(drv)
    s = drv.store.load(thread)
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert s.stage == "Stopped" and killed == ["claude"] and len(jr) == 1 and (jr[0]["job_status"], jr[0]["code"]) == ("interrupted", "stopped")


def test_schema_and_exit_failures_are_failed_jobs(tmp_path):
    cfg = bootstrap_cfg(tmp_path)
    answers = iter([("not json", 0), ('{"type":"result","is_error":true,"result":"x"}', 0), (claude_json({"status": "done"}, ["--session-id", "s"]), 0), ("", 7)])
    backend = BootstrapBackend(cfg, runner=lambda a, c, t: next(answers), sleep=lambda s: None)
    for i in range(4):
        backend.delegate("journal:C1:1.000000", contracts.DelegateRequest("explorer", f"b\nref: t/g1/i1/Explore/a{i}/explorer", tags=(f"t/g1/i1/Explore/a{i}/explorer",)))
        backend.join(10)
    backend.events(0)
    assert [(r["job_status"], r["code"]) for r in backend.journal.all() if r["kind"] == "job_result"] == [("failed", "schema"), ("failed", "is_error"), ("failed", "schema"), ("failed", "exit 7")]


def test_codex_argv_and_result_file(tmp_path):
    cfg = bootstrap_cfg(tmp_path, worker="codex", models={"explorer": "gpt-5-codex"})
    def runner(argv, cwd, timeout):
        Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(payload(result(report="codex report"))))
        return "", 0
    backend = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)
    backend.delegate("journal:C1:1.000000", contracts.DelegateRequest("explorer", "b\nref: t/g1/i1/Explore/a1/explorer", tags=("t/g1/i1/Explore/a1/explorer",)))
    backend.join(10)
    backend.events(0)
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"][0]
    job = [r for r in backend.journal.all() if r["kind"] == "job"][0]
    assert jr["job_status"] == "finished" and jr["result"]["report"] == "codex report" and job["backend"] == "codex"


# -- config and the CLI flow ----------------------------------------------------------------------------
def test_backend_config_parses_and_round_trips():
    c = config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\njournal_dir = "/tmp/j"\nworktrees_dir = "/tmp/w"\nworker = "codex"\nmax_budget_usd_per_job = 2\nmax_cost_usd_per_study = 0\nsubject_repo = "."\nretry_backoff = "1m"\n[backend.bootstrap.models]\nexplorer = "sonnet"\nimplementer = "opus"\n[backend.bootstrap.efforts]\nimplementer = "high"\n')
    b = c.backend
    assert b.kind == "bootstrap" and b.bootstrap.journal_dir == "/tmp/j" and b.bootstrap.worktrees_dir == "/tmp/w" and b.bootstrap.worker == "codex"
    assert b.bootstrap.models == {"explorer": "sonnet", "implementer": "opus"} and b.bootstrap.efforts == {"implementer": "high"}
    assert b.bootstrap.max_budget_usd_per_job == 2 and b.bootstrap.max_cost_usd_per_study == 0 and b.bootstrap.subject_repo == "." and b.bootstrap.retry_backoff == 60
    assert config.Config.from_dict(c.to_dict()) == c and config.parse("").backend == config.Backend()
    with pytest.raises(ValueError): config.parse('[backend]\nkind = "slack"\n')
    with pytest.raises(ValueError): config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nworker = "gemini"\n')
    assert isinstance(build_backend(config.parse("")), FridicaBackend) and isinstance(build_backend(dataclasses.replace(c, backend=config.Backend("bootstrap", config.Bootstrap(journal_dir="/tmp/fr-test-journal")))), BootstrapBackend)


def test_cli_start_then_serve_with_the_bootstrap_backend(tmp_path, monkeypatch, capsys):
    """`fridica-research start` writes the root into the journal; `serve --once` reads it from line 1 and delegates the explorer."""
    monkeypatch.setattr(cli, "claude_runner", lambda model: (lambda name, prompt: LLM[name]))
    calls = []
    class FakeProcessRunner:
        def __init__(self, **kw): pass  # env, on_spawn
        def __call__(self, argv, cwd, timeout):
            calls.append(argv)
            return claude_json(payload(result(report=EXPLORER_REPORT)), argv), 0
    monkeypatch.setattr("fridica_research.backend.bootstrap.ProcessRunner", FakeProcessRunner)
    p = tmp_path / "research.toml"
    p.write_text(f'channels = ["C1"]\nstate_path = "{tmp_path / "s.sqlite3"}"\n[fridica]\nowner = "{OWNER}"\n[backend]\nkind = "bootstrap"\n[backend.bootstrap]\njournal_dir = "{tmp_path / "journal"}"\n')
    assert cli.main(["--config", str(p), "start", "C1", "Study X", "--projected-hours", "2"]) == 0
    assert "posted study root to C1" in capsys.readouterr().out
    assert cli.main(["--config", str(p), "serve", "--once"]) == 0
    store = Store(tmp_path / "s.sqlite3")
    studies = store.all()
    assert len(studies) == 1 and studies[0].stage == "Explore" and studies[0].thread.startswith("journal:C1:") and studies[0].projected_hours == 2
    assert len(calls) == 1 and calls[0][0] == "claude" and "ref: " + studies[0].waiting["actions"][0]["action_id"] in calls[0][-1]
    assert cli.main(["--config", str(p), "list"]) == 0


# -- review fixes (iteration 3b): each fails on f554aea -------------------------------------------------
def explorer_req(aid: str = "t/g1/i1/Explore/a1/explorer", worker_id: str | None = None) -> contracts.DelegateRequest:
    return contracts.DelegateRequest("explorer", f"b\nref: {aid}", worker_id=worker_id, tags=(aid,))


def test_ceiling_reserves_the_budget_of_jobs_in_flight(tmp_path):
    """Debate issues two delegates at once: with ceiling 5 and 5 per job the second is refused while the first is running."""
    gate = threading.Event()
    def runner(argv, cwd, timeout):
        gate.wait(10)
        return claude_json(payload(result(report="r")), argv, cost=0.5), 0
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, max_budget_usd_per_job=5.0, max_cost_usd_per_study=5.0), runner=runner, sleep=lambda s: None)
    thread = "journal:C1:1.000000"
    backend.delegate(thread, contracts.DelegateRequest("mathematician", "b\nref: t/g1/i1/Debate/a1/debate-1-mathematician", tags=("t/g1/i1/Debate/a1/debate-1-mathematician",)))
    assert backend.spent() == 0 and backend.reserved() == 5.0
    with pytest.raises(ControlError) as e: backend.delegate(thread, contracts.DelegateRequest("physicist", "b\nref: t/g1/i1/Debate/a1/debate-1-physicist", tags=("t/g1/i1/Debate/a1/debate-1-physicist",)))
    assert (e.value.status, e.value.code) == (429, "budget_exceeded") and len([r for r in backend.journal.all() if r["kind"] == "job"]) == 1
    gate.set()
    backend.join(10)
    backend.events(0)
    assert (backend.spent(), backend.reserved()) == (0.5, 0.0)  # the result released the reservation to the actual cost


def test_result_releases_the_reservation_to_the_actual_cost(tmp_path):
    gate = threading.Event()
    def runner(argv, cwd, timeout):
        gate.wait(10)
        return claude_json(payload(result(report="r")), argv, cost=0.5), 0
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, max_budget_usd_per_job=5.0, max_cost_usd_per_study=6.0), runner=runner, sleep=lambda s: None)
    backend.delegate("journal:C1:1.000000", explorer_req("t/g1/i1/Explore/a1/explorer"))
    with pytest.raises(ControlError): backend.delegate("journal:C1:1.000000", explorer_req("t/g1/i1/Explore/a2/explorer"))  # 0 + 5 + 5 > 6
    gate.set()
    backend.join(10)
    backend.events(0)
    backend.delegate("journal:C1:1.000000", explorer_req("t/g1/i1/Explore/a3/explorer"))  # 0.5 + 0 + 5 <= 6
    backend.join(10)


SECRETS = ["sk-ant-api03-AbCdEf123456", "sk-proj-AbCdEf123456", "ghp_AbCdEf123456", "gho_AbCdEf123456", "ghs_AbCdEf123456", "ghu_AbCdEf123456",
           "github_pat_11ABCDEF0_abcdef123456", "xoxb-123-456-abcdef", "xoxp-123-456-abcdef", "xoxa-2-abcdef123", "xapp-1-A123-456-abcdef",
           "Bearer eyJhbGciOiJIUzI1NiJ9.e30.abc", "AKIAIOSFODNN7EXAMPLE", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1234\n-----END RSA PRIVATE KEY-----"]
LEAKY = "secrets: " + " | ".join(SECRETS)


def written_files(cfg) -> list[Path]:
    """Every file the backend wrote under the journal dir (the worktrees are the worker's own checkout)."""
    root = Path(cfg.backend.bootstrap.journal_dir)
    return [f for f in root.rglob("*") if f.is_file() and "worktrees" not in f.relative_to(root).parts and f.name != "journal.lock"]


def assert_no_secret(cfg):
    for f in written_files(cfg):
        text = f.read_text()
        leaked = [x for x in SECRETS if x in text or x.replace("\n", "\\n") in text]
        assert not leaked and not REDACT.search(text), (f, leaked)


@pytest.mark.parametrize("worker", ["claude", "codex"])
def test_every_file_the_backend_writes_is_redacted(tmp_path, worker):
    cfg = bootstrap_cfg(tmp_path, worker=worker)
    def runner(argv, cwd, timeout):
        assert all(x in argv[-1] for x in SECRETS)  # the worker itself gets the brief as written
        if worker == "codex":
            Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(payload(result(report=LEAKY))))
            return "", 0
        return claude_json(payload(result(report=LEAKY)), argv), 0
    backend = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)
    thread = backend.post("C1", contracts.PostRequest("study_root", f"root {LEAKY}\n\nref: r", details=LEAKY))["thread"]
    aid = "t/g1/i1/Explore/a1/explorer"
    backend.delegate(thread, contracts.DelegateRequest("explorer", f"brief {LEAKY}\nref: {aid}", tags=(aid,)))
    backend.join(10)
    backend.events(0)
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"][0]
    assert jr["job_status"] == "finished" and jr["result"]["report"].startswith("secrets: [redacted] | [redacted]")
    assert not (Path(cfg.backend.bootstrap.journal_dir) / "prompts").exists()  # the brief lives only in the redacted journal line
    names = {f.name for f in written_files(cfg)}
    assert "journal.jsonl" in names and any(n.endswith(".codex.json") for n in names) == (worker == "codex")
    assert_no_secret(cfg)


def test_redact_covers_the_token_shapes():
    for x in SECRETS: assert "[redacted]" in redact_one(x) and x not in redact_one(f"a {x} b"), x
    assert redact_one("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n") == "[redacted]"  # an unterminated block to the end
    assert redact_one("plain sk text, ghp and Bearer") == "plain sk text, ghp and Bearer"


def redact_one(text: str) -> str:
    from fridica_research.backend.bootstrap import redact
    return redact(text)


def test_a_retry_stopped_during_its_backoff_never_launches(tmp_path):
    cfg = bootstrap_cfg(tmp_path, retry_backoff=30.0)
    in_backoff, release, calls = threading.Event(), threading.Event(), []
    def sleep(s):
        in_backoff.set()
        release.wait(10)
    def runner(argv, cwd, timeout):
        calls.append(argv)
        return "", 1
    backend = BootstrapBackend(cfg, runner=runner, sleep=sleep)
    thread, aid = "journal:C1:1.000000", "t/g1/i1/Explore/a1/explorer"
    backend.delegate(thread, explorer_req(aid))
    backend.join(10)
    r = backend.delegate(thread, explorer_req(aid))  # the same ActionId again: attempt 2 backs off first
    assert in_backoff.wait(10)
    backend.stop(thread, r["jobs"][0]["worker_id"])
    release.set()
    backend.join(10)
    backend.events(0)
    assert len(calls) == 1  # the stopped retry was never started
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert [(r["attempt"], r["job_status"], r["code"]) for r in jr] == [(1, "failed", "exit 1"), (2, "interrupted", "stopped")]


def test_stopped_and_timed_out_jobs_record_their_real_cost(tmp_path):
    cfg = bootstrap_cfg(tmp_path)
    gate = threading.Event()
    class R:
        def __call__(self, argv, cwd, timeout):
            if "a1" in argv[-1]: return claude_json(payload(result(report="r")), argv, cost=0.7), None  # killed at the deadline
            gate.wait(10)
            return claude_json(payload(result(report="r")), argv, cost=0.4), None  # killed by stop
        def kill(self, argv): gate.set()
    backend = BootstrapBackend(cfg, runner=R(), sleep=lambda s: None)
    thread = "journal:C1:1.000000"
    backend.delegate(thread, explorer_req("t/g1/i1/Explore/a1/explorer"))
    backend.join(10)
    r = backend.delegate(thread, explorer_req("t/g1/i1/Explore/a2/explorer"))
    deadline = time.monotonic() + 10
    while not backend.jobs["job-2"].get("launched") and time.monotonic() < deadline: time.sleep(0.01)
    backend.stop(thread, r["jobs"][0]["worker_id"])
    backend.join(10)
    backend.events(0)
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert [(r["job_status"], r["code"], r["total_cost_usd"]) for r in jr] == [("interrupted", "timeout", 0.7), ("interrupted", "stopped", 0.4)]
    assert backend.spent() == 1.1


def test_workers_get_a_scrubbed_environment(tmp_path, monkeypatch):
    env = {"PATH": os.environ["PATH"], "HOME": "/h", "LANG": "C", "FRIDICA_CAPABILITY": "c", "GH_TOKEN": "g", "GITHUB_TOKEN": "g", "SLACK_BOT_TOKEN": "s", "SLACK_APP_LEVEL": "s",
           "NPM_TOKEN": "n", "DB_SECRET": "d", "STRIPE_API_KEY": "k", "ANTHROPIC_API_KEY": "a", "CLAUDE_CODE_OAUTH_TOKEN": "o", "ANTHROPIC_AUTH_TOKEN": "t", "OPENAI_API_KEY": "x", "CODEX_API_KEY": "y"}
    assert set(worker_env(env, "claude")) == {"PATH", "HOME", "LANG", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"}
    assert set(worker_env(env, "codex")) == {"PATH", "HOME", "LANG", "OPENAI_API_KEY", "CODEX_API_KEY"}
    for k, v in env.items(): monkeypatch.setenv(k, v)
    backend = BootstrapBackend(bootstrap_cfg(tmp_path), sleep=lambda s: None)  # the default runner
    out, rc = backend.runner(["sh", "-c", "env"], str(tmp_path), 5)
    names = {line.split("=", 1)[0] for line in out.splitlines()}
    assert rc == 0 and {"PATH", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"} <= names
    assert not names & {"FRIDICA_CAPABILITY", "GH_TOKEN", "GITHUB_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_LEVEL", "NPM_TOKEN", "DB_SECRET", "STRIPE_API_KEY", "OPENAI_API_KEY"}


def test_process_runner_reports_the_process_group(tmp_path):
    got = []
    out, rc = ProcessRunner(on_spawn=lambda argv, pgid: got.append((argv[0], pgid)))(["sh", "-c", "echo $$"], str(tmp_path), 5)
    assert rc == 0 and got == [("sh", int(out))]


def test_a_restarted_backend_kills_an_overdue_orphaned_worker_group(tmp_path):
    """The driver died with a worker running: the run file holds its pgid and absolute deadline, and a restart past it kills the group."""
    cfg = bootstrap_cfg(tmp_path, dataclasses.replace(CFG, projection={**CFG.projection, "explore": 100}))
    gate, procs = threading.Event(), []
    b1 = BootstrapBackend(cfg, runner=lambda argv, cwd, timeout: (None, None), clock=Clock(1000.0), sleep=lambda s: None)
    def runner(argv, cwd, timeout):  # a ProcessRunner whose driver is about to die: the group outlives it
        p = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30"], start_new_session=True)
        procs.append(p)
        b1._spawned(argv, p.pid)
        gate.wait(10)
        return "", None
    b1.runner = runner
    aid = "t/g1/i1/Explore/a1/explorer"
    b1.delegate("journal:C1:1.000000", explorer_req(aid))
    runs = Path(cfg.backend.bootstrap.journal_dir) / "runs"
    deadline = time.monotonic() + 10
    while not list(runs.glob("*.json")) and time.monotonic() < deadline: time.sleep(0.01)
    run = json.loads(next(runs.glob("*.json")).read_text())
    assert run["pgid"] == procs[0].pid and run["deadline"] == 1200.0 and run["job_id"] == "job-1"  # 1000 + 2 x 100 s
    try:
        BootstrapBackend(cfg, runner=runner, clock=Clock(1150.0), sleep=lambda s: None)  # restarted before the deadline: left alone
        time.sleep(0.2)
        assert procs[0].poll() is None
        b3 = BootstrapBackend(cfg, runner=runner, clock=Clock(1201.0), sleep=lambda s: None)  # restarted past it: the group is killed
        assert procs[0].wait(5) == -9 and not list(runs.glob("*.json"))
        assert b3.jobs["job-1"]["stopped"]  # still no result: the stage timer retries (rule R)
    finally:
        for p in procs:
            try: os.killpg(p.pid, 9)
            except ProcessLookupError: pass
        gate.set()
        b1.join(10)


def test_worktrees_are_removed_when_the_study_stops(tmp_path):
    sha = git_repo(tmp_path / "subject")
    cfg = bootstrap_cfg(tmp_path, subject_repo=str(tmp_path / "subject"))
    gate, cwds = threading.Event(), []
    class R:
        def __call__(self, argv, cwd, timeout):
            cwds.append(cwd)
            gate.wait(10)
            return "", None
        def kill(self, argv): gate.set()
    backend = BootstrapBackend(cfg, runner=R(), sleep=lambda s: None)
    drv = make_driver(cfg, backend)
    thread = backend.post("C1", contracts.PostRequest("study_root", "Study\n\nprojected: 1 h\ngeneration: 1\nlineage: origin\nref: r"))["thread"]
    drain(drv)
    wt = Path(cfg.backend.bootstrap.journal_dir) / "worktrees" / "w-1"
    assert wt.exists() and subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() == sha
    drv.store.command(thread, "stop")
    drain(drv)
    backend.join(10)
    drain(drv)
    assert drv.store.load(thread).stage == "Stopped" and cwds == [str(wt)]
    listed = subprocess.run(["git", "-C", str(tmp_path / "subject"), "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
    assert not wt.exists() and str(wt) not in listed


def test_codex_with_a_cost_ceiling_is_a_config_error():
    with pytest.raises(ValueError, match="codex reports no cost"):
        config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nworker = "codex"\nmax_cost_usd_per_study = 20\n')
    assert config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nworker = "claude"\nmax_cost_usd_per_study = 20\n').backend.bootstrap.max_cost_usd_per_study == 20


# -- review fixes (iteration 3c): each fails on 0eab3e7 -------------------------------------------------
def explore(backend, i: int, thread: str = "journal:C1:1.000000"):
    backend.delegate(thread, explorer_req(f"t/g1/i1/Explore/a{i}/explorer"))
    backend.join(10)
    backend.events(0)


def test_a_timed_out_job_without_a_cost_is_charged_its_reservation(tmp_path):
    """A killed `claude -p` prints nothing: the timeout must not release the reservation to $0 (ceiling 5, 5 per job)."""
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, max_budget_usd_per_job=5.0, max_cost_usd_per_study=5.0), runner=lambda a, c, t: ("", None), sleep=lambda s: None)
    explore(backend, 1)
    with pytest.raises(ControlError) as e: explore(backend, 2)
    assert (e.value.status, e.value.code) == (429, "budget_exceeded")
    with pytest.raises(ControlError): explore(backend, 3)
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert [(r["job_status"], r["code"], r["total_cost_usd"]) for r in jr] == [("interrupted", "timeout", 5.0)] and backend.spent() == 5.0


def test_a_stopped_job_without_a_cost_is_charged_its_reservation(tmp_path):
    gate = threading.Event()
    class R:
        def __call__(self, argv, cwd, timeout):
            gate.wait(10)
            return "", -9  # what the ProcessRunner returns after os.killpg
        def kill(self, argv): gate.set()
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, max_budget_usd_per_job=2.0, max_cost_usd_per_study=10.0), runner=R(), sleep=lambda s: None)
    r = backend.delegate("journal:C1:1.000000", explorer_req())
    deadline = time.monotonic() + 10
    while not backend.jobs["job-1"].get("launched") and time.monotonic() < deadline: time.sleep(0.01)
    backend.stop("journal:C1:1.000000", r["jobs"][0]["worker_id"])
    backend.join(10)
    backend.events(0)
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert [(r["job_status"], r["code"], r["total_cost_usd"]) for r in jr] == [("interrupted", "stopped", 2.0)]


def test_the_scrub_drops_the_board_token_and_cloud_database_and_agent_secrets(tmp_path, monkeypatch):
    env = {"PATH": os.environ["PATH"], "HOME": "/h", "BOARD_PAT": "b", "AWS_SECRET_ACCESS_KEY": "a", "AWS_ACCESS_KEY_ID": "a", "AWS_PROFILE": "p", "PGPASSWORD": "x", "DB_PASSWORD": "p",
           "SMTP_PASS": "p", "GOOGLE_APPLICATION_CREDENTIALS": "/c", "SSH_AUTH_SOCK": "/s", "GPG_AGENT_SOCK": "/g", "DATABASE_URL": "postgres://u:p@h/d", "SIGNING_KEY": "k",
           "ANTHROPIC_API_KEY": "a", "CLAUDE_CODE_OAUTH_TOKEN": "o", "OPENAI_API_KEY": "x"}
    kept = {"PATH", "HOME", "AWS_PROFILE", "GOOGLE_APPLICATION_CREDENTIALS"}  # non-secret provider settings (iteration 3d)
    assert set(worker_env(env, "claude", drop=("BOARD_PAT",))) == kept | {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert set(worker_env(env, "codex", drop=("BOARD_PAT",))) == kept | {"OPENAI_API_KEY"}
    # The default runner drops the board's token_env by name, whatever it is called.
    for k, v in env.items(): monkeypatch.setenv(k, v)
    cfg = bootstrap_cfg(tmp_path, dataclasses.replace(CFG, board=dataclasses.replace(CFG.board, token_env="BOARD_PAT")))
    out, rc = BootstrapBackend(cfg, sleep=lambda s: None).runner(["sh", "-c", "env"], str(tmp_path), 5)
    names = {line.split("=", 1)[0] for line in out.splitlines()}
    assert rc == 0 and {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"} <= names
    assert not names & {"BOARD_PAT", "PGPASSWORD", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "DB_PASSWORD", "SMTP_PASS", "SSH_AUTH_SOCK", "DATABASE_URL", "SIGNING_KEY"}


def test_redaction_leaves_prose_alone_and_covers_more_prefixes():
    prose = "A risk-based task-list for the disk-backed desk-top; ask-first, then mask-free. The bearer of bad news."
    assert redact_one(prose) == prose
    for x in ("bearer abcdef0123456", "BEARER abcdef0123456", "xoxe-1-abcdef123", "xoxr-abcdef123456", "ghr_AbCdEf123456", "(sk-proj-AbCdEf123456)"):
        assert "[redacted]" in redact_one(x) and x.split()[-1].strip("()") not in redact_one(x), x


def test_a_restarted_backend_leaves_a_reused_pgid_alone(tmp_path):
    """The recorded pgid now leads a process started at another time (pid reuse after a long downtime): not killed."""
    cfg = bootstrap_cfg(tmp_path, dataclasses.replace(CFG, projection={**CFG.projection, "explore": 100}))
    gate = threading.Event()
    b1 = BootstrapBackend(cfg, runner=lambda argv, cwd, timeout: (gate.wait(10), ("", None))[1], clock=Clock(1000.0), sleep=lambda s: None)
    b1.delegate("journal:C1:1.000000", explorer_req())
    stranger = subprocess.Popen(["sh", "-c", "sleep 30"], start_new_session=True)
    try:
        run = {"job_id": "job-1", "action_id": "t/g1/i1/Explore/a1/explorer", "attempt": 1, "pgid": stranger.pid, "start": "Thu Jan  1 00:00:00 1970", "deadline": 1200.0}
        runs, key = Path(cfg.backend.bootstrap.journal_dir) / "runs", action_key(run["action_id"])
        for name in (key, f"{key}-a1"): (runs / f"{name}.json").write_text(json.dumps(run))  # the 0eab3e7 name and this one
        BootstrapBackend(cfg, runner=b1.runner, clock=Clock(1201.0), sleep=lambda s: None)
        time.sleep(0.2)
        assert stranger.poll() is None
    finally:
        stranger.kill()
        gate.set()
        b1.join(10)


def test_an_orphaned_retry_is_not_answered_by_the_first_attempts_result(tmp_path):
    cfg = bootstrap_cfg(tmp_path)
    gate, calls = threading.Event(), []
    def runner(argv, cwd, timeout):
        calls.append(argv)
        if len(calls) == 1: return "", 1
        gate.wait(10)
        return "", None
    b1 = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)
    aid = "t/g1/i1/Explore/a1/explorer"
    explore_aid = lambda b: b.delegate("journal:C1:1.000000", explorer_req(aid))  # noqa: E731
    explore_aid(b1)
    b1.join(10)
    b1.events(0)
    explore_aid(b1)  # attempt 2 of the same ActionId, still running when the driver dies
    try:
        b2 = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)
        jr = [(r["job_id"], r["attempt"], r["code"]) for r in b2.journal.all() if r["kind"] == "job_result"]
        assert jr == [("job-1", 1, "exit 1")] and b2.jobs["job-2"]["orphan"]
    finally:
        gate.set()
        b1.join(10)


def wait_for(cond, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline: time.sleep(0.01)
    return cond()


@pytest.mark.parametrize("ps", ["real", "stand-in"])
def test_a_same_action_id_retry_does_not_overwrite_the_orphans_process_group(tmp_path, ps):
    """Attempt 2 of an ActionId waits for attempt 1's live orphan (L4), launches once that orphan is gone, and keeps its own run
    file: a later restart kills neither the wrong group nor attempt 2's before its own deadline. Deterministic: every step waits
    for the state it needs. With the `ps` stand-in the orphan is reported alive even after its kill (a zombie that `ps` still
    lists), so the retry is seen to wait on the liveness check, not on timing; it launches once `ps` reports it gone."""
    cfg = bootstrap_cfg(tmp_path, dataclasses.replace(CFG, projection={**CFG.projection, "explore": 100}))
    gate, procs, aid, starts = threading.Event(), [], "t/g1/i1/Explore/a1/explorer", {}
    proc_start = (lambda pid: starts.get(int(pid))) if ps == "stand-in" else process_start
    def spawning(backend):
        def runner(argv, cwd, timeout):
            p = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30"], start_new_session=True)
            starts[p.pid] = f"started {p.pid}"
            procs.append(p)
            backend._spawned(argv, p.pid)
            gate.wait(20)
            return "", None
        return runner
    def backend_at(t, spawn=False):
        b = BootstrapBackend(cfg, clock=Clock(t), sleep=lambda s: time.sleep(0.01), runner=lambda a, c, t: ("", None), proc_start=proc_start)
        if spawn: b.runner = spawning(b)
        return b
    runs = Path(cfg.backend.bootstrap.journal_dir) / "runs"
    b1 = backend_at(1000.0, spawn=True)
    b1.delegate("journal:C1:1.000000", explorer_req(aid))  # attempt 1: deadline 1200; the driver dies
    assert wait_for(lambda: (runs / f"{run_key(aid, 1)}.json").exists())
    b2 = None
    try:
        b2 = backend_at(1100.0, spawn=True)
        b2.delegate("journal:C1:1.000000", explorer_req(aid))  # attempt 2 of the same ActionId: deadline 1300 once launched; this driver dies too
        time.sleep(0.2)
        assert len(procs) == 1 and not b2.jobs["job-2"]["launched"]  # waits for the live orphan
        if ps == "real":
            backend_at(1250.0)  # a restart past attempt 1's deadline kills its group
            assert procs[0].wait(5) == -9 and not (runs / f"{run_key(aid, 1)}.json").exists()
        else:
            os.killpg(procs[0].pid, 9)  # the orphan's group is gone, but `ps` still reports its leader alive
            assert procs[0].wait(5) == -9
            time.sleep(0.2)
            assert len(procs) == 1 and not b2.jobs["job-2"]["launched"]  # so the retry still waits
            starts.pop(procs[0].pid)  # now `ps` reports it gone
        assert wait_for(lambda: len(procs) == 2 and (runs / f"{run_key(aid, 2)}.json").exists())  # the retry launched once the orphan was gone
        run2 = json.loads((runs / f"{run_key(aid, 2)}.json").read_text())
        assert run2["pgid"] == procs[1].pid and run2["deadline"] == 1300.0 and run2["attempt"] == 2
        b4 = backend_at(1250.0)  # another restart, before attempt 2's deadline: its group and run file stay
        assert b4.jobs["job-2"]["orphan"] and procs[1].poll() is None and (runs / f"{run_key(aid, 2)}.json").exists()
    finally:
        for p in procs:
            try: os.killpg(p.pid, 9)
            except (ProcessLookupError, PermissionError): pass
            p.wait(5)
        gate.set()
        b1.join(10)
        if b2: b2.join(10)


def test_codex_needs_an_explicit_zero_ceiling():
    with pytest.raises(ValueError, match="max_cost_usd_per_study = 0"):
        config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nworker = "codex"\n')
    c = config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nworker = "codex"\nmax_cost_usd_per_study = 0\n')
    assert c.backend.bootstrap.max_cost_usd_per_study == 0


def test_a_zero_ceiling_is_no_ceiling(tmp_path):
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, worker="codex", max_cost_usd_per_study=0.0), runner=lambda a, c, t: ("", 1), sleep=lambda s: None)
    explore(backend, 1)
    explore(backend, 2)
    assert len([r for r in backend.journal.all() if r["kind"] == "job_result"]) == 2


def test_unreadable_worker_output_is_a_failed_job_that_releases_its_reservation(tmp_path):
    cfg = bootstrap_cfg(tmp_path, worker="codex", max_budget_usd_per_job=5.0, max_cost_usd_per_study=100.0)
    def runner(argv, cwd, timeout):
        Path(argv[argv.index("--output-last-message") + 1]).write_bytes(b'{"status": "done", "report": "\xff\xfe"}')
        return "", 0
    backend = BootstrapBackend(cfg, runner=runner, sleep=lambda s: None)
    explore(backend, 1)
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert [(r["job_status"], r["code"], r["total_cost_usd"]) for r in jr] == [("failed", "worker_output_unreadable", 5.0)]
    assert backend.reserved() == 0 and not backend.jobs and not list((Path(cfg.backend.bootstrap.journal_dir) / "runs").iterdir())


def test_release_keeps_a_worktree_with_a_running_job_until_it_ends(tmp_path):
    cfg = bootstrap_cfg(tmp_path)
    gate = threading.Event()
    backend = BootstrapBackend(cfg, runner=lambda a, c, t: (gate.wait(10), ("", None))[1], sleep=lambda s: None)
    thread = "journal:C1:1.000000"
    backend.delegate(thread, explorer_req())
    wt = Path(cfg.backend.bootstrap.journal_dir) / "worktrees" / "w-1"
    assert wt.exists()
    assert backend.release(thread) == {"removed": []} and wt.exists()  # the worker still runs in it
    gate.set()
    backend.join(10)
    backend.events(0)
    assert not wt.exists()


# -- review fixes (iteration 3d): each behavioural one fails on a45db19 ---------------------------------
@pytest.mark.parametrize("answer", [("", -9), ("", 1), ('{"type":"result","total_cost_usd":0.4,"structured_output":{"sta', 0)], ids=["signal", "crash", "truncated"])
def test_a_launched_job_that_failed_without_a_cost_is_charged_its_reservation(tmp_path, answer):
    """B1: killed by a signal not from stop, a nonzero exit, or rc 0 with truncated output: no parseable cost, so the job is
    charged its full reservation, and with ceiling 5 and 5 per job the second delegate is refused."""
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, max_budget_usd_per_job=5.0, max_cost_usd_per_study=5.0), runner=lambda a, c, t: answer, sleep=lambda s: None)
    explore(backend, 1)
    with pytest.raises(ControlError) as e: explore(backend, 2)
    assert (e.value.status, e.value.code) == (429, "budget_exceeded")
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert [(r["job_status"], r["total_cost_usd"]) for r in jr] == [("failed", 5.0)] and (backend.spent(), backend.reserved()) == (5.0, 0.0)


def test_a_failed_job_with_a_reported_cost_and_a_launch_error_are_not_charged_the_reservation(tmp_path):
    """B1's boundary: a failed job that reported its cost is charged that cost; a runner that never launched is charged nothing."""
    answers = iter([('{"type":"result","is_error":true,"total_cost_usd":0.3}', 0), ("", "runner: no such file")])
    def runner(a, c, t):
        out, rc = next(answers)
        if isinstance(rc, str): raise FileNotFoundError("claude")
        return out, rc
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, max_budget_usd_per_job=5.0, max_cost_usd_per_study=50.0), runner=runner, sleep=lambda s: None)
    explore(backend, 1)
    explore(backend, 2)
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert [(r["code"], r["total_cost_usd"]) for r in jr] == [("is_error", 0.3), ("runner: claude", 0.0)]


@pytest.mark.parametrize("value", ["nan", "-1", "-inf", "inf"])
def test_a_non_finite_or_negative_ceiling_is_a_config_error(value):
    """B2: nan, a negative or an infinite ceiling would turn the check off silently."""
    with pytest.raises(ValueError, match="max_cost_usd_per_study must be a finite number"):
        config.parse(f'[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nmax_cost_usd_per_study = {value}\n')
    with pytest.raises(ValueError, match="max_budget_usd_per_job must be a finite number"):
        config.parse(f'[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nmax_budget_usd_per_job = {value}\n')


def test_exactly_zero_is_no_ceiling_at_load():
    c = config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nmax_cost_usd_per_study = 0\n')
    assert c.backend.bootstrap.max_cost_usd_per_study == 0


def test_redaction_catches_a_key_after_an_underscore():
    """L1: `KEY_sk-proj-...` is redacted; "risk-based" is not."""
    assert redact_one("KEY_sk-proj-abcd1234") == "KEY_[redacted]"
    assert redact_one("a risk-based plan") == "a risk-based plan"


def test_bedrock_and_vertex_settings_and_keep_env_survive_the_scrub(tmp_path, monkeypatch):
    """L2: the non-secret provider settings stay by default, `keep_env` keeps more by name, a secret still goes."""
    provider = {"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1", "AWS_PROFILE": "p", "GOOGLE_APPLICATION_CREDENTIALS": "/c.json",
                "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "us-east5", "ANTHROPIC_VERTEX_PROJECT_ID": "proj"}
    env = {"PATH": os.environ["PATH"], **provider, "AWS_SECRET_ACCESS_KEY": "s", "AWS_SESSION_TOKEN": "t", "MY_PROXY_TOKEN": "m", "GH_TOKEN": "g"}
    assert set(worker_env(env, "claude")) == {"PATH", *provider}
    assert set(worker_env(env, "claude", keep_env=("MY_PROXY_TOKEN",))) == {"PATH", "MY_PROXY_TOKEN", *provider}
    c = config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nkeep_env = ["MY_PROXY_TOKEN"]\n')
    assert c.backend.bootstrap.keep_env == ("MY_PROXY_TOKEN",) and config.Config.from_dict(json.loads(json.dumps(c.to_dict()))) == c
    with pytest.raises(ValueError, match="keep_env"): config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nkeep_env = "X"\n')
    for k, v in env.items(): monkeypatch.setenv(k, v)
    cfg = dataclasses.replace(c, state_path=str(tmp_path / "s.sqlite3"), backend=config.Backend("bootstrap", dataclasses.replace(c.backend.bootstrap, journal_dir=str(tmp_path / "journal"))))
    out, rc = BootstrapBackend(cfg, sleep=lambda s: None).runner(["sh", "-c", "env"], str(tmp_path), 5)  # the default runner
    names = {line.split("=", 1)[0] for line in out.splitlines()}
    assert rc == 0 and {"MY_PROXY_TOKEN", *provider} <= names and not names & {"AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GH_TOKEN"}


def live_orphan(cfg, aid="t/g1/i1/Explore/a1/explorer"):
    """A driver that died with attempt 1 of `aid` running: a real process group recorded in `runs/` with its start time."""
    gate, procs = threading.Event(), []
    b1 = BootstrapBackend(cfg, clock=Clock(1000.0), sleep=lambda s: None, runner=lambda a, c, t: ("", None))
    def runner(argv, cwd, timeout):
        p = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30"], start_new_session=True)
        procs.append(p)
        b1._spawned(argv, p.pid)
        gate.wait(20)
        return "", None
    b1.runner = runner
    b1.delegate("journal:C1:1.000000", explorer_req(aid))
    runs = Path(cfg.backend.bootstrap.journal_dir) / "runs"
    deadline = time.monotonic() + 10
    while not list(runs.glob("*.json")) and time.monotonic() < deadline: time.sleep(0.01)
    def cleanup():
        for p in procs:
            try: os.killpg(p.pid, 9)
            except (ProcessLookupError, PermissionError): pass  # already reaped
            p.wait(5)
        gate.set()
        b1.join(10)
    return procs, cleanup


def test_release_keeps_the_worktree_of_a_live_orphan(tmp_path):
    """L3: a run file whose group is alive per the start-time check is skipped like a running job; removed once it is gone."""
    cfg = bootstrap_cfg(tmp_path, dataclasses.replace(CFG, projection={**CFG.projection, "explore": 100}))
    procs, cleanup = live_orphan(cfg)
    try:
        b2 = BootstrapBackend(cfg, clock=Clock(1100.0), sleep=lambda s: None, runner=lambda a, c, t: ("", None))
        wt = Path(cfg.backend.bootstrap.journal_dir) / "worktrees" / "w-1"
        assert b2.jobs["job-1"]["orphan"] and wt.exists()
        assert b2.release("journal:C1:1.000000") == {"removed": []} and wt.exists() and procs[0].poll() is None
        b2.clock = Clock(1201.0)
        b2.events(0)  # past the deadline: the orphan's group is killed, then its worktree goes
        assert procs[0].wait(5) == -9 and not wt.exists()
    finally:
        cleanup()


def test_a_retry_waits_for_a_live_orphan_of_the_same_action_id(tmp_path):
    """L4: after a crash the retry of an ActionId whose orphan still runs does not launch (two workers would `--resume` one session)."""
    cfg = bootstrap_cfg(tmp_path)
    procs, cleanup = live_orphan(cfg)
    try:
        polls, launched = [], []
        def sleep(s):
            polls.append(s)
            if len(polls) == 3:  # the orphan ends (killed by the owner, or done)
                os.killpg(procs[0].pid, 9)
                procs[0].wait(5)
        def runner(argv, cwd, timeout):
            launched.append(procs[0].poll())
            return claude_json(payload(result(report="r")), argv), 0
        b2 = BootstrapBackend(cfg, runner=runner, clock=Clock(1100.0), sleep=sleep)  # before the orphan's deadline
        assert b2.jobs["job-1"]["orphan"]
        b2.delegate("journal:C1:1.000000", explorer_req("t/g1/i1/Explore/a1/explorer"))  # attempt 2: --resume of the same session
        b2.join(10)
        assert launched == [-9] and len(polls) == 3  # launched only after the orphan was gone
    finally:
        cleanup()


def test_a_retry_waiting_for_a_live_orphan_can_be_stopped(tmp_path):
    cfg = bootstrap_cfg(tmp_path)
    procs, cleanup = live_orphan(cfg)
    try:
        launched, waiting = [], threading.Event()
        def sleep(s):
            waiting.set()
            time.sleep(0.01)
        b2 = BootstrapBackend(cfg, runner=lambda a, c, t: (launched.append(a), ("", 0))[1], clock=Clock(1100.0), sleep=sleep)
        r = b2.delegate("journal:C1:1.000000", explorer_req("t/g1/i1/Explore/a1/explorer"))
        assert waiting.wait(10)
        b2.stop("journal:C1:1.000000", r["jobs"][0]["worker_id"])
        b2.join(10)
        b2.events(0)
        jr = [x for x in b2.journal.all() if x["kind"] == "job_result"]
        assert not launched and [(x["attempt"], x["code"]) for x in jr] == [(2, "stopped")] and procs[0].poll() is None
    finally:
        cleanup()


# -- review fixes (iteration 3e): each behavioural one fails on 0c005ba ---------------------------------
FAKE_CLAUDE = {  # a worker that ran, reported its cost, and wrote invalid UTF-8
    "stderr-0xff": "printf '%s' '{\"type\":\"result\",\"total_cost_usd\":3.0,\"is_error\":true}'\nprintf '\\377' >&2\n",
    "stdout-truncated-multibyte": "printf '%s' '{\"type\":\"result\",\"total_cost_usd\":3.0,\"is_error\":true,\"result\":\"'\nprintf '\\342\\202'\nprintf '\"}'\n",
}


@pytest.mark.parametrize("script", list(FAKE_CLAUDE), ids=list(FAKE_CLAUDE))
def test_a_worker_that_wrote_invalid_utf8_is_charged_not_a_launch_error(tmp_path, monkeypatch, script):
    """M1: the default ProcessRunner reads invalid UTF-8 with replacement, so the reported cost (3.0) is charged; with ceiling 5 and
    5 per job the second delegate is refused (on 0c005ba the decode error was a launch error at $0 and the reservation was released)."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "claude").write_text("#!/bin/sh\n" + FAKE_CLAUDE[script])
    (bin_ / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, max_budget_usd_per_job=5.0, max_cost_usd_per_study=5.0), sleep=lambda s: None)  # the default runner
    explore(backend, 1)
    with pytest.raises(ControlError) as e: explore(backend, 2)
    assert (e.value.status, e.value.code) == (429, "budget_exceeded")
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert [(r["job_status"], r["code"], r["total_cost_usd"]) for r in jr] == [("failed", "is_error", 3.0)] and (backend.spent(), backend.reserved()) == (3.0, 0.0)


def test_a_runner_exception_after_the_spawn_is_charged_its_reservation(tmp_path):
    """M1: an exception raised after on_spawn (the worker process exists) is a failed job charged its full reservation; one raised
    before the spawn stays a launch error at $0 (test_a_failed_job_with_a_reported_cost_and_a_launch_error_are_not_charged_the_reservation)."""
    holder = {}
    def runner(argv, cwd, timeout):
        holder["b"]._spawned(argv, os.getpid())
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, max_budget_usd_per_job=5.0, max_cost_usd_per_study=5.0), runner=runner, sleep=lambda s: None, proc_start=lambda pid: None)
    holder["b"] = backend
    explore(backend, 1)
    with pytest.raises(ControlError) as e: explore(backend, 2)
    assert (e.value.status, e.value.code) == (429, "budget_exceeded")
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert len(jr) == 1 and jr[0]["job_status"] == "failed" and jr[0]["code"].startswith("runner: ") and jr[0]["total_cost_usd"] == 5.0
    assert not list((Path(backend.dir) / "runs").iterdir())


def test_a_zero_per_job_budget_with_a_ceiling_is_a_config_error():
    """M2: max_budget_usd_per_job = 0 with a positive ceiling makes the reservation and the B1 charge $0."""
    with pytest.raises(ValueError, match="max_budget_usd_per_job must be > 0"):
        config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nmax_budget_usd_per_job = 0\nmax_cost_usd_per_study = 20\n')
    c = config.parse('[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nworker = "codex"\nmax_budget_usd_per_job = 0\nmax_cost_usd_per_study = 0\n')  # no ceiling: allowed
    assert c.backend.bootstrap.max_budget_usd_per_job == 0


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN", "BOARD_PAT", "FRIDICA_CAPABILITY", "SLACK_BOT_TOKEN", "DB_SECRET", "AWS_SECRET_ACCESS_KEY", "MY_SECRET_THING"])
def test_keep_env_may_not_keep_the_board_token_or_the_drivers_secrets(name):
    """L1: keep_env rejects the board's token_env and FRIDICA_*, SLACK_*, GH_TOKEN, GITHUB_TOKEN, *_SECRET*."""
    with pytest.raises(ValueError, match="keep_env may not keep"):
        config.parse(f'[board]\ntoken_env = "BOARD_PAT"\n[backend]\nkind = "bootstrap"\n[backend.bootstrap]\nkeep_env = ["MY_PROXY_TOKEN", "{name}"]\n')
    assert config.parse('[board]\ntoken_env = "BOARD_PAT"\n[backend.bootstrap]\nkeep_env = ["MY_PROXY_TOKEN"]\n').backend.bootstrap.keep_env == ("MY_PROXY_TOKEN",)


@pytest.mark.parametrize("cost", ["NaN", "-1.0", "Infinity"])
def test_a_nan_or_negative_cost_is_unknown_and_charged_the_reservation(tmp_path, cost):
    """L2: Python's json reads NaN; a NaN, infinite or negative cost would turn the ceiling off, so it is charged the reservation."""
    out = f'{{"type":"result","is_error":true,"total_cost_usd":{cost}}}'
    backend = BootstrapBackend(bootstrap_cfg(tmp_path, max_budget_usd_per_job=5.0, max_cost_usd_per_study=5.0), runner=lambda a, c, t: (out, 0), sleep=lambda s: None)
    explore(backend, 1)
    with pytest.raises(ControlError) as e: explore(backend, 2)
    assert (e.value.status, e.value.code) == (429, "budget_exceeded")
    jr = [r for r in backend.journal.all() if r["kind"] == "job_result"]
    assert [(r["code"], r["total_cost_usd"]) for r in jr] == [("is_error", 5.0)] and backend.spent() == 5.0


@pytest.mark.parametrize("bad", [{"max_cost_usd_per_study": float("nan")}, {"max_cost_usd_per_study": -1.0}, {"max_budget_usd_per_job": 0.0},
                                 {"max_budget_usd_per_job": True}, {"max_cost_usd_per_study": True}, {"retry_backoff": True}, {"worker": "codex"},
                                 {"keep_env": ["GH_TOKEN"]}, {"worker": "gemini"}], ids=lambda d: f"{next(iter(d))}={next(iter(d.values()))}")
def test_from_dict_runs_the_same_checks_as_parse(bad):
    """L3: the replay path (Config.from_dict) validates like parse, and a boolean is not a number there or in TOML."""
    d = json.loads(json.dumps(config.parse('[backend]\nkind = "bootstrap"\n').to_dict()))
    assert config.Config.from_dict(d) == config.parse('[backend]\nkind = "bootstrap"\n')
    d["backend"]["bootstrap"].update(bad)
    with pytest.raises(ValueError): config.Config.from_dict(json.loads(json.dumps(d)))


@pytest.mark.parametrize("key", ["max_budget_usd_per_job", "max_cost_usd_per_study", "retry_backoff"])
def test_a_boolean_for_a_numeric_bootstrap_field_is_a_config_error(key):
    with pytest.raises(ValueError, match="boolean"):
        config.parse(f'[backend]\nkind = "bootstrap"\n[backend.bootstrap]\n{key} = true\n')
