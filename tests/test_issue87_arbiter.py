"""#87: the arbiter's merge-ready path, CHANGES_REQUESTED in Implement, and the closing keywords (part of #89 and #91).

Pure folds over `arbiter.observe`: no driver, no store, no GitHub.
"""
from __future__ import annotations

import pytest

from fridica_research import arbiter, config
from fridica_research.machine import Event

THREAD = "T1:C1:1700000000.000100"
REPO = "chengcli/fridica-research"
IMP, AUD, DRV = "UIMP", "UAUD", "UDRV"
HEAD, NEW = "a" * 40, "b" * 40
T0 = 1_791_800_000.0


def rnd(stage: str, head: str = HEAD, driver: bool = True) -> arbiter.Round:
    roster = {"implementer": {"holders": [IMP], "login": "imp", "delegate": False},
              "auditor": {"holders": [AUD], "login": "aud", "delegate": False}}
    if driver: roster["driver"] = {"holders": [DRV], "login": "drv", "delegate": False}
    return arbiter.Round(thread=THREAD, channel="C1", issue=87, repo=REPO, stage=stage, roster=roster,
                         pr={"number": 70, "head": head, "state": "OPEN", "draft": False})


def rv(n: int, state: str, commit: str, login: str = "aud") -> dict:
    return {"id": str(n), "n": n, "state": state, "commit": commit, "login": login, "at": f"2026-10-12T1{n % 10}:00:00Z"}


def poll(r: arbiter.Round, reviews: list[dict], head: str = HEAD, now: float = T0):
    return arbiter.observe(r, Event("pr_polled", now, {"view": {"headRefOid": head, "state": "OPEN"}, "reviews": reviews}), config.Config())


def posts(actions) -> list[str]: return [a.data["text"] for a in actions if a.kind == "post"]


# -- (1) the first approval on the current head moves Implement or CodeAudit to Merge --------
@pytest.mark.parametrize("stage", ["Implement", "CodeAudit"])
def test_first_approval_on_the_current_head_moves_to_merge(stage):
    r, actions = poll(rnd(stage), [rv(1, "APPROVED", HEAD)])
    assert r.stage == "Merge" and r.clearance["CodeAudit"]["review"] == "1"


def test_an_approval_on_an_old_head_does_not_move():
    r, actions = poll(rnd("CodeAudit"), [rv(1, "APPROVED", HEAD)], head=NEW)
    assert r.stage == "CodeAudit" and actions == []


def test_the_held_hand_off_path_stays():
    r = rnd("CodeAudit")
    r.held["CodeAudit"] = {"ts": "1.1", "sender": AUD, "text": "Code audit done, handoff: https://x/y\nDue 10-12 12:00 ET"}
    r, _ = poll(r, [rv(1, "APPROVED", HEAD)])
    assert r.stage == "Merge" and r.held == {} and r.clearance["CodeAudit"]["author"] == AUD and r.clearance["CodeAudit"]["handoff_ts"] == "1.1"


# -- (2) "ready to merge" once per head, from move() into Merge --------------------------
def test_ready_to_merge_is_posted_once_per_head():
    r, a1 = poll(rnd("CodeAudit"), [rv(1, "APPROVED", HEAD)])
    assert len(posts(a1)) == 1 and "ready to merge" in posts(a1)[0] and HEAD[:12] in posts(a1)[0]
    assert [a for a in a1 if a.kind == "post"][0].data in [{k: v for k, v in p.items() if k not in ("kind", "id")} for p in r.pending]
    r, a2 = poll(r, [rv(1, "APPROVED", HEAD)], now=T0 + 60)
    assert posts(a2) == []  # the same head never posts twice
    r.stage = "CodeAudit"  # bounced and re-approved on a new head: a new post
    r, a3 = poll(r, [rv(2, "APPROVED", NEW)], head=NEW, now=T0 + 120)
    assert len(posts(a3)) == 1 and NEW[:12] in posts(a3)[0]


def test_ready_to_merge_comes_from_move_into_merge():
    r = rnd("CodeAudit")
    actions = arbiter.move(r, "CodeAudit", "Merge", T0, "k", config.Config())
    assert r.stage == "Merge" and len(posts(actions)) == 1 and "ready to merge" in posts(actions)[0]
    assert posts(arbiter.move(rnd("Implement"), "Implement", "CodeAudit", T0, "k", config.Config())) == []


# -- (3) no "no driver" finding; the auditor stays assignee at Merge --------------------
def test_no_driver_finding_and_the_auditor_stays_assignee_at_merge():
    r = rnd("CodeAudit", driver=False)
    r.held["CodeAudit"] = {"ts": "1.1", "sender": AUD, "text": "Code audit done, handoff: https://x/y\nDue 10-12 12:00 ET"}
    r, actions = poll(r, [rv(1, "APPROVED", HEAD)])
    assert r.stage == "Merge" and not any("driver" in f for f in r.findings)
    board = [a for a in actions if a.kind == "board_update"][0]
    assert board.data["assignee"] == "aud" and board.data["remove_assignees"] == []
    assert not any("holder unset" in p for p in posts(actions))


def test_with_a_driver_in_the_roster_the_auditor_still_stays_assignee():
    r, actions = poll(rnd("Implement"), [rv(1, "APPROVED", HEAD)])
    board = [a for a in actions if a.kind == "board_update"][0]
    assert board.data["assignee"] == "aud" and board.data["options"]["Stage"] == "Deliver"


# -- (5) CHANGES_REQUESTED in Implement too, and no budget due line ----------------------
def test_changes_requested_in_implement_is_handled_once_and_stays_in_implement():
    r, actions = poll(rnd("Implement"), [rv(4, "CHANGES_REQUESTED", HEAD)])
    assert r.stage == "Implement" and r.pr["changes_requested"] == "4"
    assert len(posts(actions)) == 1 and f"<@{IMP}>" in posts(actions)[0]
    r, again = poll(r, [rv(4, "CHANGES_REQUESTED", HEAD)], now=T0 + 60)
    assert posts(again) == []


@pytest.mark.parametrize("stage", ["Implement", "CodeAudit"])
def test_changes_post_has_no_budget_due_line_and_says_the_floor_manager_sets_the_eta(stage):
    _, actions = poll(rnd(stage), [rv(4, "CHANGES_REQUESTED", HEAD)])
    text = posts(actions)[0]
    assert "The floor manager sets the fix ETA." in text and "Due " not in text


# -- (6) closing keywords --------------------------------------------------------------
@pytest.mark.parametrize("kw", ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved", "refs", "Closes", "FIXES"])
def test_closing_keywords_and_refs_are_accepted(kw):
    assert arbiter.closes_or_refs(f"{kw} #87", 87, REPO)
    assert arbiter.closes_or_refs(f"{kw} {REPO}#87", 87, REPO)
    assert not arbiter.closes_or_refs(f"{kw} #870", 87, REPO)
    assert not arbiter.closes_or_refs(f"{kw} other/repo#87", 87, REPO)


@pytest.mark.parametrize("body", ["fixing #87", "closer #87", "prefix #87", "ref #87", "resolving #87", "Part of #87"])
def test_near_misses_are_not_keywords(body):
    assert not arbiter.closes_or_refs(body, 87, REPO)
