"""The physicist's five discriminating tests (debate-physicist.md): restart equivalence, claim ordering,
progress notes that look like claims, slot refusal at the iteration boundary, egress-refused deliverable."""
from __future__ import annotations

import dataclasses
import os

import pytest

from fridica_research.backend.fridica import FridicaBackend
from fridica_research.client import Client
from fridica_research.driver import Driver
from fridica_research.store import Store
from fake_control import FakeControl
from support import CFG, EXPLORER_REPORT, LLM, OWNER, PEER, World, report, result
from test_driver import Clock, drain, root_text

CLAIM_ALPHA = "Claim (iteration 1): Alpha design\napproach: alpha\nwhy: w\nalso considered: none"


@pytest.fixture
def world(tmp_path, sock_dir):
    sock = os.path.join(sock_dir, "c.sock")
    server = FakeControl(sock, owner=OWNER).start()
    cfg = dataclasses.replace(CFG, socket=sock, state_path=str(tmp_path / "state.sqlite3"), settle_window=0)
    yield server, cfg
    server.stop()


def driver(cfg, store=None, clock=None):
    store = store or Store(cfg.state_file)
    if store.cursor is None: store.set_cursor(0)
    return Driver(cfg, FridicaBackend(Client(cfg.socket_path)), store, llm=lambda n, p: LLM[n], clock=clock or Clock(), sleep=lambda s: None)


def to_mid_debate2(server, cfg, drv, thread):
    server.root(thread, OWNER, root_text())
    drain(drv)
    server.finish_job(thread, server.job_of(thread, "explorer")["id"], result(report=EXPLORER_REPORT))
    drain(drv)
    for role in ("mathematician", "physicist"): server.finish_job(thread, server.job_of(thread, role)["id"], result(report=report(position="disagree")))
    drain(drv)
    assert drv.store.load(thread).round == 2 and drv.store.load(thread).stage == "Debate"


# 1. Restart equivalence: the next delegate body is byte-identical with and without the restart.
def test_restart_equivalence_next_delegate_is_byte_identical(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    drv = driver(cfg)
    to_mid_debate2(server, cfg, drv, thread)
    jobs_before = len(server.view(thread)["jobs"])
    # Uninterrupted driver: finish round 2 with agreement -> synthesis -> implementer delegate.
    snapshot = drv.store.load(thread).to_dict()
    for role in ("mathematician", "physicist"): server.finish_job(thread, server.job_of(thread, role)["id"], result(report=report(position="agree")))
    cursor_at_results = len(server.events)
    drain(drv)
    uninterrupted = [r for r in server.requests if r[1].endswith("/delegate")][-1]
    assert uninterrupted[2]["role"] == "implementer"
    # Kill it: roll the world back to the snapshot and replay through a fresh driver with a fresh store.
    server.view(thread)["jobs"] = server.view(thread)["jobs"][:jobs_before]
    for j in server.view(thread)["jobs"][-2:]: j["job_status"], j["result"] = "finished", result(report=report(position="agree"))
    del server.requests[:]
    store = Store(cfg.state_file)
    store.save(dataclasses.replace(drv.store.load(thread), **{k: v for k, v in snapshot.items() if k != "thread"}), cursor_at_results - 2)
    drv2 = driver(cfg, store=store)
    drain(drv2)
    restarted = [r for r in server.requests if r[1].endswith("/delegate")]
    assert len(restarted) == 1 and restarted[0][2] == uninterrupted[2]
    assert sum(1 for j in server.view(thread)["jobs"] if j["role"] == "explorer") == 1  # iteration count not lost
    assert sum(1 for m in server.view(thread)["messages"] if m["meta"] and m["meta"]["kind"] == "study_claim") == 1  # claim not re-posted


# 2. Claim ordering: echo after peer, and echo before peer.
def test_claim_ordering_peer_before_echo_we_win():
    w = World(auto_post=False)
    w.to_claim()
    post = w.kinds("post")[0]
    w.ev("peer_post", ts="1700000000.000900", sender=PEER, kind="study_claim", text=CLAIM_ALPHA)  # T1
    assert w.state.claim["status"] == "pending" and not w.kinds("delegate")[1:]
    w.ev("own_post_seen", ts="1700000000.000800", kind="study_claim", text=post["text"])  # T0 < T1
    assert w.state.claim["slug"] == "alpha" and w.state.claim["status"] == "settling"
    w.tick(w.cfg.settle_window)
    assert w.state.stage == "Debate" and [a["role"] for a in w.kinds("delegate")] == ["explorer", "mathematician", "physicist"]


def test_claim_ordering_peer_earlier_we_repick_and_delegate_nothing_in_between():
    w = World(auto_post=False)
    w.to_claim()
    post = w.kinds("post")[0]
    w.ev("peer_post", ts="1700000000.000700", sender=PEER, kind="study_claim", text=CLAIM_ALPHA)  # T1
    w.ev("own_post_seen", ts="1700000000.000800", kind="study_claim", text=post["text"])  # T0 > T1: lost
    claims = [p for p in w.kinds("post") if p["post_kind"] == "study_claim"]
    assert len(claims) == 2 and "approach: beta" in claims[1]["text"] and w.state.claim["status"] == "pending"
    assert [a["role"] for a in w.kinds("delegate")] == ["explorer"]
    w.ev("own_post_seen", ts="1700000000.000900", kind="study_claim", text=claims[1]["text"])
    w.tick(w.cfg.settle_window)
    assert w.state.stage == "Debate" and len([a for a in w.kinds("delegate") if a["role"] == "mathematician"]) == 1


def test_claim_ordering_echo_then_peer_earlier_during_settle():
    w = World(auto_post=False)
    w.to_claim()
    w.ev("own_post_seen", ts="1700000000.000800", kind="study_claim", text=w.kinds("post")[0]["text"])
    w.ev("peer_post", ts="1700000000.000700", sender=PEER, kind="study_claim", text=CLAIM_ALPHA)
    assert w.state.excluded == ["alpha"] and w.state.claim["slug"] == "beta"


# 3. Progress notes and human text are not claims.
def test_progress_notes_and_humans_are_not_claims(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    drv = driver(cfg)
    server.root(thread, OWNER, root_text())
    drain(drv)
    server.message(thread, PEER, CLAIM_ALPHA, kind="progress")
    server.message(thread, "UHUMAN", CLAIM_ALPHA)
    server.message(thread, OWNER, CLAIM_ALPHA, kind="progress")  # our own progress note
    drain(drv)
    s = drv.store.load(thread)
    assert s.peer_claims == {} and s.claim is None
    server.finish_job(thread, server.job_of(thread, "explorer")["id"], result(report=EXPLORER_REPORT))
    drain(drv)
    assert drv.store.load(thread).claim["slug"] == "alpha"


# 4. Slot pressure at the iteration boundary: 409 once, job.interrupted, then 200; same body re-sent.
def test_slot_refusal_waits_for_interrupted_and_retries_same_request(world):
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    drv = driver(cfg)
    server.root(thread, OWNER, root_text())
    drain(drv)
    server.finish_job(thread, server.job_of(thread, "explorer")["id"], result(report=EXPLORER_REPORT))
    server.refuse_delegate = [(409, "too_many_workers")]
    drain(drv)
    s = drv.store.load(thread)
    assert s.stage == "Debate" and s.phase == "slot" and s.attempt == 1
    refused = [r for r in server.requests if r[1].endswith("/delegate")][-2:]
    assert [r[2]["role"] for r in refused] == ["mathematician", "physicist"]  # one refused, one accepted
    accepted = {r[2]["role"] for r in refused} - {s.waiting["refused"]}
    assert len(accepted) == 1
    # An unrelated worker's job is interrupted: the slot frees.
    server.view(thread)["jobs"].append({"id": "job-old", "worker_id": "w-old", "role": "implementer", "brief": "", "tags": [], "job_status": "running", "result": None, "error": None, "inbox_id": "g0", "attempt": 1})
    server.finish_job(thread, "job-old", None, status="interrupted", code="stopped")
    drain(drv)
    s = drv.store.load(thread)
    assert s.phase == "job" and s.attempt == 1 and not s.notes
    sent = [r[2] for r in server.requests if r[1].endswith("/delegate")]
    assert sent[-1] == next(r[2] for r in refused if r[2]["role"] == s.waiting["refused"])  # byte-identical re-send
    assert {j["role"] for j in server.view(thread)["jobs"] if j["job_status"] == "running"} == {"mathematician", "physicist"}


def test_other_4xx_on_delegate_is_a_stage_failure():
    w = World()
    w.to_claim()
    w.refuse_delegate = ["unknown_role"]
    w.tick(w.cfg.settle_window)
    assert w.state.attempt == 2 and w.state.stage == "Debate" and w.state.phase == "job"


# 5. Refused post is not a stage failure.
def test_egress_refused_deliverable_is_redacted_not_rerun():
    w = World(hold=("study_result",))
    w.to_delivered()
    assert w.state.stage == "Deliver" and w.state.waiting["action"]["post_kind"] == "study_result"
    first = w.kinds("post")[-1]
    assert first["details"] == "long details"
    n_delegates, n_llm = len(w.kinds("delegate")), len(w.kinds("llm_call"))
    w.ev("post_refused", post_kind="study_result", code="egress_deny_list_3", outcome="rejected")
    second = w.kinds("post")[-1]
    assert second["post_kind"] == "study_result" and second["details"] is None and "withheld by the egress gate" in second["text"]
    assert w.state.stage == "Deliver" and w.state.redacted and len(w.kinds("delegate")) == n_delegates and len(w.kinds("llm_call")) == n_llm
    assert "egress" in w.kinds("notify_owner")[-1]["text"]
    w.ev("own_post_seen", ts=w.next_ts(), kind="study_result", text=second["text"])
    assert w.state.stage == "Delivered" and w.kinds("post")[-1]["post_kind"] == "study_root"


def test_rate_limited_post_retries_after_delay_without_llm():
    w = World(hold=("study_result",))
    w.to_delivered()
    n_llm = len(w.kinds("llm_call"))
    w.ev("post_refused", post_kind="study_result", code="rate_limited", outcome="failed", retry_after=90)
    assert w.state.stage == "Deliver" and any(t.endswith("/repost") for t in w.state.timers)
    w.tick(90)
    posts = [p for p in w.kinds("post") if p["post_kind"] == "study_result"]
    assert len(posts) == 2 and posts[0]["text"] == posts[1]["text"] and len(w.kinds("llm_call")) == n_llm


def test_second_egress_refusal_of_same_post_is_a_stage_failure():
    w = World(hold=("study_result",))
    w.to_delivered()
    w.ev("post_refused", post_kind="study_result", code="egress_ai_trailer", outcome="rejected")
    w.ev("post_refused", post_kind="study_result", code="egress_ai_trailer", outcome="rejected")
    assert w.state.attempt == 2 and w.state.stage == "Deliver" and w.state.phase == "post" and w.kinds("llm_call")[-1]["name"] == "study_deliver"
