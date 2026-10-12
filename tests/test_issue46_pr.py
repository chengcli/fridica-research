"""#46 slice 2: PR adoption and polling for an adopted round (ref fm-wave-c-job1).

The round adopts the implementer's own open PR (drafts too) whose body has `Closes #N` or `Refs #N` as a
whole word; the PR is read once per pass while the stage is Implement or CodeAudit. Implement clears only
with a recorded PR, CodeAudit only on the auditor's latest review being APPROVED on the current head; a
Slack SIGN-OFF is recorded but never clears; CHANGES_REQUESTED on the head sends the round back to
Implement with one post keyed by review id, and a later approval clears through CodeAudit.
"""
from __future__ import annotations

import json

from fridica_research import arbiter, github
from fridica_research.board import Board
from fridica_research.machine import Event
from test_board import FakeGh
import test_issue46_guards as guards
from test_issue46_guards import AUDITOR, IMPLEMENTER, ISSUE, LINK, REPO, THREAD, Clock, adopted, drain, make, own_posts, rnd, round_meta

world = guards.world  # the guards' fixture: a FakeControl daemon and a board-enabled config

OLD, NEW = "1" * 40, "2" * 40
HANDOFF_TO_AUDIT = f"Implement handoff: <@{AUDITOR}> over to you.\n{LINK}\nDue 10-12 09:00 ET"
HANDOFF_TO_MERGE = f"Code audit done, handoff to the driver: <@UDRV> over to you.\n{LINK}\nDue 10-12 09:00 ET"


class PrGh(FakeGh):
    """FakeGh that also answers `gh pr list`, `gh pr view` and the REST reviews call."""

    def __init__(self):
        super().__init__()
        self.prs: list[dict] = []
        self.head = OLD
        self.reviews: list[dict] = []

    def __call__(self, argv, stdin):
        if argv[:3] in (["gh", "pr", "list"], ["gh", "pr", "view"]) or (argv[:2] == ["gh", "api"] and "/reviews" in " ".join(argv)):
            self.calls.append((argv, None))
            if self.fail_once and self.fail_once in " ".join(argv):
                self.fail_once = None
                raise RuntimeError("gh failed once")
            if argv[2] == "list": return json.dumps(self.prs)
            if argv[2] == "view": return json.dumps({"headRefOid": self.head, "state": "OPEN", "isDraft": False})
            return json.dumps(self.reviews)
        return super().__call__(argv, stdin)

    def reads(self) -> list[str]:
        return [" ".join(a[:3]) for a, _ in self.calls if a[:3] in (["gh", "pr", "list"], ["gh", "pr", "view"]) or "/reviews" in " ".join(a)]


def pr(number: int, login: str, body: str, draft: bool = False) -> dict:
    return {"number": number, "headRefOid": OLD, "state": "OPEN", "isDraft": draft, "author": {"login": login}, "body": body}


def review(rid: int, login: str, state: str, commit: str, at: str) -> dict:
    return {"id": rid, "user": {"login": login, "type": "User"}, "state": state, "commit_id": commit, "submitted_at": at, "body": ""}


RECORDED = {"number": 70, "head": OLD, "state": "OPEN", "draft": False}


def setup(world, stage: str, pr_: dict | None = None):
    server, cfg = world
    store, calls = adopted(cfg, round_meta(stage=stage, pr=dict(pr_ or {})))
    gh = PrGh()
    drv, _ = make(cfg, store, Clock(), calls, gh)
    return server, store, drv, gh


# -- adoption ----------------------------------------------------------------------------
def test_closes_or_refs_is_a_whole_word_and_only_for_the_rounds_repo():
    yes = ["Closes #46", "Refs #46 (slices 3 and 4 remain)", "refs chengcli/fridica-research#46", "Closes #46.", "x\nRefs #46\n"]
    no = ["Closes #4", "Closes #460", "Refs #461x", "Fixed46 #46", "Refs other/repo#46", "#46", "Refs #46-b", "Refs #4 6"]
    assert all(arbiter.closes_or_refs(b, 46, REPO) for b in yes)
    assert not any(arbiter.closes_or_refs(b, 46, REPO) for b in no)


def test_adopts_the_implementers_own_open_pr_including_a_draft(world):
    server, store, drv, gh = setup(world, "Implement")
    gh.prs = [pr(80, "someone", f"Closes #{ISSUE}"), pr(81, "imp", f"Closes #{ISSUE}0"), pr(82, "imp", f"Refs #{ISSUE}", draft=True),
              pr(83, "imp", f"Refs other/repo#{ISSUE}")]
    drain(drv)
    assert rnd(store)["pr"] == {"number": 82, "head": OLD, "state": "OPEN", "draft": True}
    assert any(a[:3] == ["gh", "pr", "list"] and "imp" in a for a, _ in gh.calls)


def test_one_read_per_round_per_pass_and_only_in_implement_or_code_audit(world):
    server, store, drv, gh = setup(world, "Implement")
    gh.prs = [pr(82, "imp", f"Closes #{ISSUE}")]
    drv.run_once()
    assert gh.reads() == ["gh pr list"]  # before adoption: the list only
    drv.run_once()
    assert gh.reads() == ["gh pr list", "gh pr view", "gh api --paginate"]  # after: view plus reviews, once
    r = json.loads(store.get_meta(f"round:{THREAD}"))
    r["stage"] = "Merge"
    store.set_meta(f"round:{THREAD}", json.dumps(r))
    drv.run_once()
    assert len(gh.reads()) == 3  # Merge is not polled


def test_a_failed_read_is_logged_and_retried_next_pass(world, caplog):
    server, store, drv, gh = setup(world, "Implement")
    gh.prs = [pr(82, "imp", f"Closes #{ISSUE}")]
    gh.fail_once = "pr list"
    drv.run_once()
    assert rnd(store)["pr"] == {} and "pr read for round" in caplog.text
    drv.run_once()
    assert rnd(store)["pr"]["number"] == 82


# -- Implement -> CodeAudit --------------------------------------------------------------
def test_implement_handoff_is_held_until_a_pr_is_recorded(world):
    server, store, drv, gh = setup(world, "Implement")
    server.message(THREAD, IMPLEMENTER, HANDOFF_TO_AUDIT)
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Implement" and "Implement" in r["held"]
    assert any("held" in f and "no PR" in f for f in r["findings"]) and own_posts(server) == []
    gh.prs = [pr(82, "imp", f"Refs #{ISSUE}")]
    drain(drv)
    r = rnd(store)
    assert r["pr"]["number"] == 82 and r["stage"] == "CodeAudit" and r["held"] == {}


# -- CodeAudit ---------------------------------------------------------------------------
def test_code_audit_clears_only_on_the_auditors_approval_of_the_current_head(world):
    server, store, drv, gh = setup(world, "CodeAudit", RECORDED)
    gh.head = NEW
    gh.reviews = [review(1, "aud", "APPROVED", OLD, "2026-10-11T10:00:00Z"),  # stale: the head moved
                  review(2, "imp", "APPROVED", NEW, "2026-10-11T11:00:00Z")]  # not the auditor
    server.message(THREAD, AUDITOR, f"SIGN-OFF #70 {NEW[:7]} approve")
    server.message(THREAD, AUDITOR, HANDOFF_TO_MERGE)
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "CodeAudit" and "CodeAudit" in r["held"]
    assert r["pr"]["signoffs"][0]["verdict"] == "approve" and r["pr"]["head"] == NEW  # the Slack SIGN-OFF is recorded, never a clearance
    assert any("held" in f and "APPROVED" in f for f in r["findings"])
    assert own_posts(server) == []  # held silently
    gh.reviews.append(review(3, "aud", "APPROVED", NEW, "2026-10-11T12:00:00Z"))
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Merge" and r["held"] == {} and r["clearance"]["CodeAudit"]["author"] == AUDITOR  # the held hand-off replayed
    posts = own_posts(server)  # #87: the move into Merge posts "ready to merge" once for the head, and nothing else
    assert len(posts) == 1 and "ready to merge" in posts[0] and NEW[:12] in posts[0]


def test_changes_requested_sends_the_round_back_once_and_an_approval_then_clears_through(world):
    server, store, drv, gh = setup(world, "CodeAudit", RECORDED)
    gh.reviews = [review(5, "aud", "CHANGES_REQUESTED", OLD, "2026-10-11T10:00:00Z")]
    for _ in range(3): drain(drv)
    r = rnd(store)
    assert r["stage"] == "Implement" and r["pr"]["changes_requested"] == "5"
    posts = own_posts(server)
    assert len(posts) == 1 and f"<@{IMPLEMENTER}>" in posts[0] and f"ref: {THREAD}/round/changes/5" in posts[0]
    gh.head = NEW
    gh.reviews.append(review(6, "aud", "APPROVED", NEW, "2026-10-11T12:00:00Z"))
    drain(drv)
    r = rnd(store)
    assert r["stage"] == "Merge" and r["clearance"]["CodeAudit"]["review"] == "6"  # cleared through, no second hand-off
    posts = own_posts(server)
    assert len(posts) == 2 and "ready to merge" in posts[1]  # #87: the changes post, then one "ready to merge" for the new head


def test_a_changes_request_on_a_stale_head_does_nothing(world):
    server, store, drv, gh = setup(world, "CodeAudit", RECORDED)
    gh.head = NEW
    gh.reviews = [review(5, "aud", "CHANGES_REQUESTED", OLD, "2026-10-11T10:00:00Z")]
    drain(drv)
    assert rnd(store)["stage"] == "CodeAudit" and own_posts(server) == []


# -- the PR on Round ---------------------------------------------------------------------
def test_the_pr_is_a_plain_dict_that_round_trips_through_replay(world):
    server, store, drv, gh = setup(world, "CodeAudit", RECORDED)
    gh.reviews = [review(5, "aud", "CHANGES_REQUESTED", OLD, "2026-10-11T10:00:00Z")]
    server.message(THREAD, AUDITOR, f"SIGN-OFF #70 {OLD[:7]} changes")
    drain(drv)
    raw = store.get_meta(f"round:{THREAD}")
    back = arbiter.Round.from_json(raw)
    assert type(back.pr) is dict and back.to_json() == raw and json.loads(json.dumps(back.pr)) == back.pr
    rs = github.pr_reviews(gh, f"{REPO}#70")
    again, actions = arbiter.observe(back, Event("pr_polled", drv.clock(), {"view": {"headRefOid": OLD, "state": "OPEN", "isDraft": False}, "reviews": rs}), drv.cfg)
    assert again.to_json() == raw and actions == []  # the same read again changes nothing


# -- #61 note: the marker match keeps study T:1 from matching T:10 ---------------------------
def test_marker_for_study_t1_never_matches_study_t10(world):
    _, cfg = world
    gh = FakeGh()
    board = Board(cfg, runner=gh)
    marker = "Adopted as the study card of fridica-research study T:1"
    gh.add_comment(REPO, ISSUE, "Adopted as the study card of fridica-research study T:10 (generation 1).", gh.login)
    assert board.api.find_comment(ISSUE, marker) is None
    c = gh.add_comment(REPO, ISSUE, marker + " (generation 1).", gh.login)
    assert board.api.find_comment(ISSUE, marker) == c["id"]
