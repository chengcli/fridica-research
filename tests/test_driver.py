"""Driver tests against the fake control server: feed translation, execution, persistence, restart."""
from __future__ import annotations

import dataclasses
import os

import pytest

from fridica_research.client import Client
from fridica_research.config import Config
from fridica_research.driver import Driver
from fridica_research.store import Store
from fake_control import FakeControl
from support import CFG, EXPLORER_REPORT, LLM, OWNER, PEER, REV, report, result


class Clock:
    def __init__(self, t=1_700_000_000.0): self.t = t
    def __call__(self): return self.t


@pytest.fixture
def world(tmp_path, sock_dir):
    sock = os.path.join(sock_dir, "c.sock")
    server = FakeControl(sock, owner=OWNER).start()
    cfg = dataclasses.replace(CFG, socket=sock, state_path=str(tmp_path / "state.sqlite3"))
    yield server, cfg
    server.stop()


def make_driver(cfg: Config, store: Store | None = None, clock: Clock | None = None, llm=None) -> Driver:
    store = store or Store(cfg.state_file)
    if store.cursor is None: store.set_cursor(0)
    return Driver(cfg, Client(cfg.socket_path), store, llm=llm or (lambda n, p: LLM[n]), clock=clock or Clock(), sleep=lambda s: None)


def drain(drv: Driver, limit: int = 10):
    """Poll until a pass applies no feed event (own echoes land on the page after the one that caused them)."""
    for _ in range(limit):
        n, at_end = drv.run_once()
        if n == 0 and at_end: return
    raise AssertionError("feed did not settle")


def root_text(text="Study the thing", generation=1, lineage="origin", hours=4): return f"{text}\n\nprojected: {hours} h\ngeneration: {generation}\nlineage: {lineage}\nref: start"


def test_root_post_starts_a_study_and_runs_to_claim(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    drv = make_driver(cfg)
    drain(drv)
    s = drv.store.load(thread)
    assert s.stage == "Explore" and s.phase == "job" and s.projected_hours == 4 and s.generation == 1 and s.spawner
    assert server.drivers[thread] == "external"
    job = server.job_of(thread, "explorer")
    assert "ref: " + s.waiting["id"] in job["brief"]
    delegate_body = next(body for method, path, body in server.requests if method == "POST" and path.endswith("/delegate"))
    assert "# Explorer" in delegate_body["instructions"]
    server.finish_job(thread, job["id"], result(report=EXPLORER_REPORT))
    drain(drv)
    s = drv.store.load(thread)
    assert s.stage == "Claim" and s.claim["status"] == "settling"  # the fake echoed the claim as the owner's message
    assert drv.store.cursor == len(server.events)


def test_peer_root_without_generation_line_is_never_followed(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000200"
    server.root(thread, PEER, "A peer's study")
    drv = make_driver(cfg)
    drain(drv)
    s = drv.store.load(thread)
    assert s.generation == cfg.max_generations and not s.spawner


def test_messages_outside_research_channels_or_from_strangers_are_ignored(world):
    server, cfg = world
    server.root("T1:C9:1700000000.000300", OWNER, root_text())
    server.root("T1:C1:1700000000.000400", "USTRANGER", root_text(), kind=None)
    drv = make_driver(cfg)
    drain(drv)
    assert drv.store.all() == []


def test_human_signoff_and_login_replies_are_translated(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    drv = make_driver(cfg)
    drain(drv)
    server.message(thread, REV, "SIGN-OFF https://github.com/o/r/pull/9 abc1234 approve")
    server.message(thread, REV, "@reviewer-login")
    server.message(thread, "UHUMAN", "looks good to me")
    drain(drv)
    s = drv.store.load(thread)
    assert s.signoffs == {REV: "approve"} and s.people == {REV: "reviewer-login"}


def test_owner_stop_command_is_applied(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    drv = make_driver(cfg)
    drain(drv)
    drv.store.command(thread, "stop")
    drain(drv)
    s = drv.store.load(thread)
    assert s.stage == "Stopped" and not s.workers["explorer"]["live"]
    assert any(p == "POST" and path.endswith("/w-1/stop") for p, path, _ in server.requests)
    assert server.job_of(thread, "explorer")["job_status"] == "interrupted"


def test_timers_fire_from_stored_deadlines(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    clock = Clock()
    drv = make_driver(cfg, clock=clock)
    drain(drv)
    clock.t += cfg.stage_timeout + 1
    drain(drv)
    s = drv.store.load(thread)
    assert s.attempt == 2 and len([j for j in server.view(thread)["jobs"] if j["role"] == "explorer"]) == 2


def test_llm_failure_is_a_retryable_stage_failure(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    calls = []
    def llm(name, prompt):
        calls.append(name)
        if len(calls) == 1: raise RuntimeError("boom")
        return LLM[name]
    drv = make_driver(cfg, llm=llm)
    drain(drv)
    s = drv.store.load(thread)
    assert s.attempt == 2 and s.phase == "job" and calls == ["study_brief", "study_brief"]


def test_restart_resumes_waiting_group_without_redelegating(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    drv = make_driver(cfg)
    drain(drv)
    n_jobs = len(server.view(thread)["jobs"])
    drv2 = make_driver(cfg, store=Store(cfg.state_file))
    drain(drv2)
    assert len(server.view(thread)["jobs"]) == n_jobs
    assert drv2.store.load(thread).to_dict() == drv.store.load(thread).to_dict()


def test_restart_resends_lost_delegate(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    drv = make_driver(cfg)
    drain(drv)
    s = drv.store.load(thread)
    server.view(thread)["jobs"].clear()  # the POST was lost after the snapshot
    s.group["jobs"].clear()
    drv.store.save(s)
    drv2 = make_driver(cfg, store=Store(cfg.state_file))
    drain(drv2)
    jobs = server.view(thread)["jobs"]
    assert len(jobs) == 1 and jobs[0]["role"] == "explorer" and "ref: " + s.waiting["id"] in jobs[0]["brief"]


def test_restart_synthesises_own_post_seen_from_thread_view(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    drv = make_driver(cfg)
    drain(drv)
    server.finish_job(thread, server.job_of(thread, "explorer")["id"], result(report=EXPLORER_REPORT))
    # Apply the job result but hide the claim echo from the feed by trimming the events after it.
    drain(drv)
    s = drv.store.load(thread)
    assert s.claim["status"] == "settling"
    s.claim.update(status="pending", ts=None)
    s.phase, s.waiting = "post", {"kind": "post", "id": s.waiting["id"].rsplit("/", 1)[0] + "/claim", "action": {"thread": thread, "post_kind": "study_claim", "text": server.view(thread)["messages"][-1]["text"], "details": None}}
    drv.store.save(s)
    drv2 = make_driver(cfg, store=Store(cfg.state_file))
    drain(drv2)
    s2 = drv2.store.load(thread)
    assert s2.claim["status"] == "settling" and s2.claim["ts"] == server.view(thread)["messages"][-1]["ts"]
    assert sum(1 for m in server.view(thread)["messages"] if m["meta"] and m["meta"]["kind"] == "study_claim") == 1


def test_full_study_through_the_driver(world):
    server, cfg = world
    cfg = dataclasses.replace(cfg, settle_window=0)
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    drv = make_driver(cfg)
    drain(drv)
    server.finish_job(thread, server.job_of(thread, "explorer")["id"], result(report=EXPLORER_REPORT))
    drain(drv)  # settle timer (0 s) fires -> Debate
    s = drv.store.load(thread)
    assert s.stage == "Debate"
    for role in ("mathematician", "physicist"): server.finish_job(thread, server.job_of(thread, role)["id"], result(report=report(position="agree")))
    drain(drv)
    server.finish_job(thread, server.job_of(thread, "implementer")["id"], result(artifacts=["https://github.com/o/r/pull/9"]))
    drain(drv)
    server.finish_job(thread, server.job_of(thread, "auditor")["id"], result(report=report(verdict="pass")))
    drain(drv)
    s = drv.store.load(thread)
    assert s.stage == "Delivered" and s.followon
    kinds = [m["meta"]["kind"] for m in server.view(thread)["messages"] if m["meta"]]
    assert kinds == ["study_root", "study_claim", "report", "study_result"]
    child = drv.store.load(s.followon)
    assert child and child.generation == 2 and child.lineage == thread and child.stage == "Explore"


def test_restart_mid_debate_pair_adopts_existing_jobs_and_resends_only_the_missing_one(world):
    server, cfg = world
    cfg = dataclasses.replace(cfg, settle_window=0)
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    drv = make_driver(cfg)
    drain(drv)
    server.finish_job(thread, server.job_of(thread, "explorer")["id"], result(report=EXPLORER_REPORT))
    drain(drv)
    s = drv.store.load(thread)
    assert s.stage == "Debate" and len(s.group["jobs"]) == 2
    # Crash after the snapshot that emitted the pair but before any `delegated` was applied; the physicist's POST was lost.
    s.group["jobs"].clear()
    s.group["pending"] = ["mathematician", "physicist"]
    drv.store.save(s)
    server.view(thread)["jobs"] = [j for j in server.view(thread)["jobs"] if j["id"] != server.job_of(thread, "physicist")["id"]]
    drv2 = make_driver(cfg, store=Store(cfg.state_file))
    drain(drv2)
    s2 = drv2.store.load(thread)
    assert sorted(j["role"] for j in server.view(thread)["jobs"] if j["job_status"] == "running") == ["debater", "debater"]
    assert sorted(j["role"] for j in s2.group["jobs"].values()) == ["mathematician", "physicist"]
    assert s2.workers["mathematician"]["worker_id"] == server.job_of(thread, "mathematician")["worker_id"]  # adopted, not re-delegated
    assert s2.phase == "job" and s2.waiting["kind"] == "group"
