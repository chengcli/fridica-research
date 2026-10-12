"""#66 R22 cross-repo as a pure module (ref fm-wave-e-job3): `file_issue` and `track`.

The fakes are `gh pr view --json number,url,author,state,reviewDecision,latestReviews,headRefOid` in its
real shape (R34): `latestReviews` entries carry an empty `id` and `commit.oid`, and `reviewDecision` is ""
when the repo requires no review.
"""
from __future__ import annotations

import dataclasses

import pytest

from fridica_research import config, crossrepo

URL = "https://github.com/chengcli/snapy/pull/90"
HEAD, HEAD2 = "21970279e713fccfd32543281ea3209e9ea62c19", "3" * 40
CFG = dataclasses.replace(config.Config(), repos=(config.Repo("chengcli/snapy"),),
                          board=config.Board(enabled=True, owner="chengcli", number=8, repo="chengcli/fridica-research"))
THREAD = "T1:C1:1791809216.197149"


def latest(login: str, state: str) -> dict:
    """One `latestReviews` element as `gh pr view` returns it: id and commit oid empty."""
    return {"id": "", "author": {"login": login}, "authorAssociation": "COLLABORATOR", "body": "...", "submittedAt": "2026-10-12T13:00:00Z",
            "includesCreatedEdit": False, "reactionGroups": [], "state": state, "commit": {"oid": ""}}


def view(state="OPEN", reviews=(), decision="", head=HEAD) -> dict:
    return {"number": 90, "url": URL, "author": {"id": "MDQ6VXNlcjE=", "is_bot": False, "login": "impl", "name": "Imp Lementer"},
            "state": state, "reviewDecision": decision, "latestReviews": list(reviews), "headRefOid": head}


def kinds(actions) -> list[str]: return [a.kind for a in actions]


# -- track ---------------------------------------------------------------------------------
def test_first_poll_is_a_baseline_card_update_only():
    s, actions = crossrepo.track(None, view(reviews=[latest("rev", "CHANGES_REQUESTED")], decision="CHANGES_REQUESTED"))
    assert kinds(actions) == ["card_update"] and s["head"] == HEAD


def test_no_change_means_no_actions():
    s, _ = crossrepo.track(None, view(reviews=[latest("rev", "COMMENTED")]))
    assert crossrepo.track(s, view(reviews=[latest("rev", "COMMENTED")])) == (s, [])


@pytest.mark.parametrize("after,changed", [
    (view(state="MERGED"), ["state"]),
    (view(state="CLOSED"), ["state"]),
    (view(reviews=[latest("rev", "APPROVED")]), ["reviews"]),
    (view(decision="REVIEW_REQUIRED"), ["decision"]),
    (view(head=HEAD2), ["head"]),
])
def test_each_change_notifies_the_implementer_and_updates_the_card(after, changed):
    s, _ = crossrepo.track(None, view())
    _, actions = crossrepo.track(s, after)
    assert kinds(actions) == ["card_update", "notify_implementer"]
    assert actions[1]["changed"] == changed and actions[1]["to"] == "impl"


def test_a_new_changes_requested_records_a_finding():
    s, _ = crossrepo.track(None, view(reviews=[latest("rev", "COMMENTED")]))
    s, actions = crossrepo.track(s, view(reviews=[latest("rev", "CHANGES_REQUESTED")], decision="CHANGES_REQUESTED"))
    assert kinds(actions) == ["card_update", "finding", "notify_implementer"] and "rev requested changes" in actions[1]["text"]
    _, again = crossrepo.track(s, view(reviews=[latest("rev", "CHANGES_REQUESTED")], decision="CHANGES_REQUESTED", head=HEAD2))
    assert "finding" not in kinds(again)  # the same request, not a new one


def test_only_the_implementer_is_notified():
    s, _ = crossrepo.track(None, view())
    _, actions = crossrepo.track(s, view(reviews=[latest("rev", "CHANGES_REQUESTED"), latest("other", "APPROVED")], decision="CHANGES_REQUESTED", head=HEAD2))
    notes = [a for a in actions if a.kind == "notify_implementer"]
    assert len(notes) == 1 and notes[0]["to"] == "impl"
    assert all("rev" not in str(a.get("to", "")) and "other" not in str(a.get("to", "")) for a in actions)


def test_review_changes_are_detected_from_login_and_state_with_empty_id_and_oid():
    s, _ = crossrepo.track(None, view(reviews=[latest("rev", "COMMENTED")]))
    assert s["reviews"] == [["rev", "COMMENTED"]]
    _, actions = crossrepo.track(s, view(reviews=[latest("rev", "APPROVED")]))  # id and oid are "" before and after
    assert actions[1]["changed"] == ["reviews"]
    _, same = crossrepo.track(s, view(reviews=[{**latest("rev", "COMMENTED"), "submittedAt": "2026-10-12T14:00:00Z", "body": "again"}]))
    assert same == []


def test_review_decision_empty_when_no_review_is_required():
    s, actions = crossrepo.track(None, view(decision=""))
    assert s["decision"] == "" and actions[0]["decision"] == ""
    _, actions = crossrepo.track(s, view(decision="", reviews=[latest("rev", "APPROVED")]))
    assert actions[1]["changed"] == ["reviews"] and "(none required)" in actions[1]["text"]


def test_no_review_request_action_ever():
    s = None
    seq = [view(), view(reviews=[latest("rev", "CHANGES_REQUESTED")], decision="CHANGES_REQUESTED"), view(head=HEAD2, decision="REVIEW_REQUIRED"),
           view(head=HEAD2, reviews=[latest("rev", "APPROVED")], decision="APPROVED"), view(state="MERGED", head=HEAD2, decision="APPROVED")]
    for v in seq:
        s, actions = crossrepo.track(s, v)
        for a in actions:
            assert a.kind in crossrepo.ACTION_KINDS
            assert "request" not in a.kind and "review" not in a.kind and "remind" not in a.kind
            assert "--add-reviewer" not in str(a.data) and "requested_reviewers" not in str(a.data)


# -- file_issue -----------------------------------------------------------------------------
def test_file_issue_builds_the_gh_argv():
    argv = crossrepo.file_issue("chengcli/snapy", "Carry: face-in operator", "The study needs X.", CFG, thread=THREAD, contract="C1: residual < 1e-6")
    assert argv[:7] == ["gh", "issue", "create", "-R", "chengcli/snapy", "--title", "Carry: face-in operator"] and argv[7] == "--body"
    body = argv[8]
    assert f"Study thread: {THREAD}" in body.splitlines()
    assert "Board: https://github.com/users/chengcli/projects/8" in body.splitlines()
    assert "C1: residual < 1e-6" in body and body.startswith("The study needs X.")


def test_file_issue_refuses_a_repo_not_in_repos():
    with pytest.raises(ValueError, match="not in \\[repos\\]"):
        crossrepo.file_issue("someone/else", "t", "b", CFG, thread=THREAD, contract="c")


def test_file_issue_needs_the_thread_and_the_contract():
    with pytest.raises(ValueError, match="study thread"):
        crossrepo.file_issue("chengcli/snapy", "t", "b", CFG, thread="", contract="c")
    with pytest.raises(ValueError, match="contract"):
        crossrepo.file_issue("chengcli/snapy", "t", "b", CFG, thread=THREAD, contract=" ")


# -- audit fixes (fm-wave-e-job3-fix) ---------------------------------------------------------
def test_an_empty_prev_is_a_baseline_too():
    s, actions = crossrepo.track({}, view(reviews=[latest("rev", "CHANGES_REQUESTED")], decision="CHANGES_REQUESTED"))
    assert kinds(actions) == ["card_update"] and s["head"] == HEAD


def test_action_ids_are_unique_per_transition_at_the_same_head():
    def ids(seq):
        s, out = None, []
        for v in seq:
            s, actions = crossrepo.track(s, v)
            out += [a.id for a in actions]
        return out
    comment_then_approve = [view(), view(reviews=[latest("rev", "COMMENTED")]), view(reviews=[latest("rev", "APPROVED")])]
    got = ids(comment_then_approve)
    assert len(got) == 5 and len(set(got)) == len(got)
    cr = view(reviews=[latest("rev", "CHANGES_REQUESTED")], decision="CHANGES_REQUESTED")
    cr_dismissed_cr = [view(), cr, view(reviews=[latest("rev", "DISMISSED")]), {**cr, "latestReviews": [{**latest("rev", "CHANGES_REQUESTED"), "submittedAt": "2026-10-12T14:00:00Z"}]}]
    got = ids(cr_dismissed_cr)
    assert len(set(got)) == len(got)
    assert sum("/changes/" in i for i in got) == 2  # one finding per CHANGES_REQUESTED, each with its own id
