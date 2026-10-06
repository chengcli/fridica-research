"""GitHub sign-off (R21), PR hygiene (R23), driver-side merge and late reviews (R24) with a fake `gh` runner: no network."""
from __future__ import annotations

import dataclasses
import json
import os

import pytest

from fridica_research import board, config, contracts, machine, replay
from fridica_research.client import Client, ControlError
from fridica_research.config import Board as BoardCfg
from fridica_research.config import GitHub as GitHubCfg
from fridica_research.config import Repo, Reviewer
from fridica_research.driver import Driver
from fridica_research.github import GitHub, PrHygieneError, parse_iso
from fridica_research.store import Store
from fake_control import FakeControl
from support import CFG, LLM, OWNER, PR, REV, SHA, World, result

HEAD = SHA + "0" * 33  # the PR's full head; the implementer reported the short sha
OLD = "0123456" + "f" * 33
SQUASH = "5" * 40
MERGED_AT = "2026-10-04T12:00:00Z"
GCFG = dataclasses.replace(
    CFG, audit_scopes=("scope",), require_signoffs=True, reviewers=(Reviewer(REV, "scope", "reviewer"),),
    board=BoardCfg(enabled=True, owner="chengcli", number=8, repo="o/r"),
    github=GitHubCfg(enabled=True, poll_interval=0), repos=(Repo("o/r", "driver", ("reviewer", "rev2")),),
)


def rv(rid, login, state, oid=HEAD, at="2026-10-04T10:00:00Z", body="", typ="User"):
    """A review as `gh api repos/<o>/<r>/pulls/<n>/reviews` returns it (the shape of chengcli/fridica-research#18's)."""
    return {"author_association": "COLLABORATOR", "body": body, "commit_id": oid, "id": rid, "state": state, "submitted_at": at, "user": {"login": login, "type": typ}}


def latest_review(r: dict) -> dict:
    """The same review as `gh pr view --json latestReviews` returns it: the id and the commit oid are empty strings."""
    return {"id": "", "author": {"login": r["user"]["login"]}, "authorAssociation": r["author_association"], "body": r["body"], "submittedAt": r["submitted_at"], "includesCreatedEdit": False, "reactionGroups": [], "state": r["state"], "commit": {"oid": ""}}


class FakeGh:
    """Answers the gh calls of github.py with real gh output shapes from a table of PRs; records (argv, parsed stdin).

    `reviews` is the REST list (paginated two per page, pages printed back to back as `gh api --paginate` does);
    `gh pr view` answers only the requested `--json` fields, `latestReviews` in its real shape (empty id and oid)."""

    def __init__(self, *prs: tuple[str, dict]):
        self.prs = {contracts.pr_id(url): {"reviews": [], "headRefOid": HEAD, "state": "OPEN", "mergedAt": None, "mergeCommit": None, "author": {"login": "implementer"}, "milestone": None, "isDraft": False, **v} for url, v in prs}
        self.calls: list[tuple[list[str], dict | None]] = []
        self.milestones: dict[str, list[dict]] = {}
        self.opened: dict[str, str] = {}  # head branch -> PR url
        self.failures: list[tuple] = []  # (predicate on argv, error text), each raised once
        self.refuse: set[str] = set()  # logins GitHub answers 422 for

    def fail_once(self, pred, text="HTTP 502"): self.failures.append((pred, text))

    def __call__(self, argv: list[str], stdin: str | None) -> str:
        doc = json.loads(stdin) if stdin else None
        self.calls.append((argv, doc))
        for f in self.failures:
            if f[0](argv):
                self.failures.remove(f)
                raise RuntimeError(f"{' '.join(argv[:3])} failed: {f[1]}")
        if argv[:3] == ["gh", "pr", "view"]:
            pr = self.prs[(argv[argv.index("-R") + 1], argv[3])]
            full = {**pr, "latestReviews": [latest_review(r) for r in pr["reviews"]], "reviewDecision": "APPROVED" if any(r["state"] == "APPROVED" for r in pr["reviews"]) else ""}
            return json.dumps({k: full[k] for k in argv[argv.index("--json") + 1].split(",")})
        if argv[:3] == ["gh", "pr", "merge"]:
            if self.prs[(argv[argv.index("-R") + 1], argv[3])]["isDraft"]: raise RuntimeError("gh pr merge failed: Pull request #9 is still a draft")
            self.prs[(argv[argv.index("-R") + 1], argv[3])].update(state="MERGED", mergedAt=MERGED_AT, mergeCommit={"oid": SQUASH})
            return ""
        if argv[:3] == ["gh", "pr", "create"]:
            url = f"https://github.com/{argv[argv.index('-R') + 1]}/pull/{13 + len(self.opened)}"
            self.opened[argv[argv.index("--head") + 1]] = url
            return url + "\n"
        if argv[:3] == ["gh", "pr", "list"]:
            url = self.opened.get(argv[argv.index("--head") + 1])
            return json.dumps([{"url": url}] if url else [])
        if argv[:3] == ["gh", "api", "graphql"]:
            q, v = doc["query"], doc["variables"]
            if q == board.Q_DISCOVER["user"]: return json.dumps({"data": {"user": {"projectV2": {"id": "PVT_1", "fields": {"nodes": []}}}}})
            if "pullRequest(number" in q: return json.dumps({"data": {"repository": {"pullRequest": {"id": f"PR_{v['name']}_{v['number']}"}}}})
            if q == board.M_ADD_ITEM: return json.dumps({"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_" + v["content"]}}}})
            raise AssertionError(q[:60])
        if argv[:2] == ["gh", "api"]:
            path = next(a for a in argv[2:] if a.startswith("repos/"))
            if path.endswith("/milestones") and "POST" in argv:
                ms = self.milestones.setdefault(path, [])
                ms.append({"title": doc["title"], "number": len(ms) + 1})
                return json.dumps(ms[-1])
            if "/milestones?" in path: return json.dumps(self.milestones.get(path.split("?")[0], []))
            if "/reviews?" in path and "--paginate" in argv:
                parts = path.split("?")[0].split("/")  # repos/<o>/<r>/pulls/<n>/reviews
                rs = self.prs[("/".join(parts[1:3]), parts[4])]["reviews"]
                return "".join(json.dumps(rs[i:i + 2]) for i in range(0, len(rs), 2)) or "[]"
            if path.endswith("/requested_reviewers") and set(doc["reviewers"]) & self.refuse:
                raise RuntimeError("gh api -X failed: Reviews may only be requested from collaborators. One or more of the users or teams you specified is not a collaborator of the o/r repository. (HTTP 422)")
            return "{}"
        raise AssertionError(argv)

    def argv(self, *prefix: str) -> list[list[str]]: return [a for a, _ in self.calls if tuple(a[: len(prefix)]) == prefix]
    def api(self, path: str) -> list[tuple[list[str], dict | None]]: return [(a, d) for a, d in self.calls if a[:2] == ["gh", "api"] and path in a]


def make(gh: FakeGh, cfg=GCFG):
    meta: dict[str, str] = {}
    return GitHub(cfg, runner=gh, remember=lambda k, v: meta.__setitem__(k, v) if v is not None else meta.pop(k, None), recall=meta.get), meta


def audit_world(cfg=GCFG, pr=PR) -> World:
    w = World(cfg=cfg)
    w.to_audit(pr=pr)
    assert w.state.stage == "Audit" and w.state.phase == "signoff"
    return w


def poll(gh_client: GitHub, w: World, now: float | None = None):
    return gh_client.poll(w.state, now if now is not None else w.now, lambda item: [w.feed(ev) for ev in item.events])


# -- R21: reviews as sign-offs -----------------------------------------------------------
def test_review_requests_go_through_rest_once_per_head_never_to_the_author():
    gh = FakeGh((PR, {"author": {"login": "rev2"}}))
    g, _ = make(gh)
    w = audit_world()
    poll(g, w)
    poll(g, w)
    reqs = gh.api("repos/o/r/pulls/9/requested_reviewers")
    assert reqs == [(["gh", "api", "-X", "POST", "repos/o/r/pulls/9/requested_reviewers", "--input", "-"], {"reviewers": ["reviewer"]})]
    assert not gh.argv("gh", "pr", "edit")
    gh.prs[("o/r", "9")]["headRefOid"] = OLD  # a new push: requested again on the new head
    poll(g, w)
    assert len(gh.api("repos/o/r/pulls/9/requested_reviewers")) == 2


def test_copilot_approved_is_ignored():
    gh = FakeGh((PR, {"reviews": [rv(1, "copilot-pull-request-reviewer", "APPROVED"), rv(2, "dependabot[bot]", "APPROVED")]}))
    g, _ = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert out.events == [] and out.posts == [] and not gh.argv("gh", "pr", "merge")
    assert w.state.stage == "Audit" and w.state.audit_scopes["scope"]["signed_at"] is None


def test_approval_on_an_old_head_is_ignored():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED", oid=OLD)]}))
    g, _ = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert out.events == [] and out.posts == [] and not gh.argv("gh", "pr", "merge")
    assert w.state.stage == "Audit" and w.state.signoffs == {}


def test_approval_on_the_head_closes_the_card_and_merges_with_the_squash_sha():
    gh = FakeGh((PR, {"reviews": [rv(1, "Reviewer", "APPROVED")]}))
    g, meta = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert [e.kind for e in out.events] == ["sign_off"] and out.events[0].data == {"sender": REV, "pr": PR, "sha": HEAD, "verdict": "approve"}  # login -> Slack id, case-insensitive
    assert w.state.stage == "Delivered" and w.state.audit_scopes["scope"]["verdict"] == "approve"  # full head vs the implementer's short sha
    assert gh.argv("gh", "pr", "merge") == [["gh", "pr", "merge", "9", "-R", "o/r", "--squash", "--match-head-commit", HEAD]]
    assert meta["github:revision:o/r:g1"] == SQUASH and json.loads(meta[f"github:pr:{w.state.thread}:o/r#9"])["merged_sha"] == SQUASH
    assert out.posts[-1][1] == f"merged o/r#9 (squash) as {SQUASH} after approval by Reviewer on {HEAD[:12]}"
    poll(g, w)
    assert len(gh.argv("gh", "pr", "merge")) == 1  # merged once


def test_merge_only_for_driver_repos_and_only_without_changes_on_the_head():
    owner_cfg = dataclasses.replace(GCFG, repos=(Repo("o/r", "owner"),))
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED")]}))
    g, _ = make(gh, owner_cfg)
    w = audit_world(owner_cfg)
    out = poll(g, w)
    poll(g, w)
    assert not gh.argv("gh", "pr", "merge") and [p for _, p in out.posts if "merged by its owner" in p] == [f"{PR} approved on {HEAD[:12]} by reviewer; o/r is merged by its owner"]
    unlisted = dataclasses.replace(GCFG, repos=())  # a repository not in [repos] is the owner's
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED")]}))
    g, _ = make(gh, unlisted)
    poll(g, audit_world(unlisted))
    assert not gh.argv("gh", "pr", "merge")
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED"), rv(2, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T11:00:00Z")]}))
    g, _ = make(gh)
    poll(g, audit_world())
    assert not gh.argv("gh", "pr", "merge")
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "COMMENTED"), rv(2, "copilot", "APPROVED")]}))  # no non-bot approval
    g, _ = make(gh)
    poll(g, audit_world())
    assert not gh.argv("gh", "pr", "merge")


def test_commented_is_a_finding_without_verdict():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "COMMENTED", body="nit: rename x")]}))
    g, _ = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert [e.kind for e in out.events] == ["finding"]
    assert w.state.findings[-1] == f"iteration 1 note during Audit: GitHub review by reviewer on o/r#9 at {HEAD[:12]} (COMMENTED, no verdict): nit: rename x"
    assert w.state.signoffs == {} and w.state.audit_scopes["scope"]["signed_at"] is None and w.state.stage == "Audit"


def test_changes_requested_returns_the_study():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "CHANGES_REQUESTED")]}))
    g, _ = make(gh)
    w = audit_world()
    poll(g, w)
    assert w.state.iteration == 2 and w.state.audit["verdict"] == "return"


def test_one_mirror_line_per_review_never_parsed_back():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "COMMENTED")]}))
    g, _ = make(gh)
    w = audit_world()
    first = poll(g, w)
    gh.prs[("o/r", "9")]["reviews"].append(rv(2, "reviewer", "APPROVED", at="2026-10-04T11:00:00Z"))
    second = poll(g, w)
    third = poll(g, w)
    lines = [t for _, t in first.posts + second.posts + third.posts if t.startswith("SIGN-OFF")]
    assert lines == [f"SIGN-OFF {contracts.MIRROR_MARK} reviewer: COMMENTED on o/r#9 at {HEAD[:12]}", f"SIGN-OFF {contracts.MIRROR_MARK} reviewer: APPROVED on o/r#9 at {HEAD[:12]}"]
    for line in lines:
        assert contracts.parse_signoff(line) is None and "sign_off" not in replay.contract_lines(line)
    assert contracts.parse_signoff(f"SIGN-OFF {contracts.MIRROR_MARK} x\nSIGN-OFF {PR} {SHA} approve") is None  # the whole mirror post is never a sign-off


def test_driver_mirrors_into_the_thread_and_the_echo_is_not_a_signoff(tmp_path, sock_dir):
    sock = os.path.join(sock_dir, "c.sock")
    server = FakeControl(sock, owner=OWNER).start()
    try:
        cfg = dataclasses.replace(GCFG, socket=sock, state_path=str(tmp_path / "s.sqlite3"))
        w = audit_world(cfg)
        store = Store(cfg.state_file)
        store.save(w.state, 0, w.now)
        gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "COMMENTED", body="looks fine")]}))
        g, _ = make(gh, cfg)
        drv = Driver(cfg, Client(cfg.socket_path), store, llm=lambda n, p: LLM[n], clock=lambda: w.now, sleep=lambda s: None, github=g)
        for _ in range(3): drv.run_once()
        posts = [m for m in server.view(w.state.thread)["messages"] if contracts.MIRROR_MARK in m["text"]]
        assert len(posts) == 1 and posts[0]["text"].endswith(f"ref: {w.state.thread}/github/review-1")
        s = store.load(w.state.thread)
        assert s.signoffs == {} and s.stage == "Audit" and sum("COMMENTED" in f for f in s.findings) == 1
    finally:
        server.stop()


def test_github_disabled_polls_nothing():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED")]}))
    g, _ = make(gh, dataclasses.replace(GCFG, github=GitHubCfg()))
    assert g.poll(audit_world().state, 0).events == [] and gh.calls == []


# -- round 1 of review: B1-B4, L1-L4 ---------------------------------------------------------
# chengcli/fridica-research#18, captured 2026-10-04: `gh pr view --json headRefOid,latestReviews,reviewDecision` and one
# element of `gh api repos/chengcli/fridica-research/pulls/18/reviews`, verbatim.
REAL_VIEW = {"headRefOid": "21970279e713fccfd32543281ea3209e9ea62c19", "latestReviews": [{"id": "", "author": {"login": "UCzhangxi"}, "authorAssociation": "COLLABORATOR", "body": "...", "submittedAt": "2026-10-04T18:05:17Z", "includesCreatedEdit": False, "reactionGroups": [], "state": "APPROVED", "commit": {"oid": ""}}], "reviewDecision": "APPROVED"}
REAL_REVIEW = {"author_association": "COLLABORATOR", "body": "...", "commit_id": "21970279e713fccfd32543281ea3209e9ea62c19", "id": 5407449127, "state": "APPROVED", "submitted_at": "2026-10-04T18:05:17Z", "user": {"login": "UCzhangxi", "type": "User"}}


def test_b1_reviews_bind_to_the_head_through_rest_commit_id_never_latest_reviews():
    real_head = REAL_VIEW["headRefOid"]
    cfg = dataclasses.replace(GCFG, reviewers=(Reviewer(REV, "scope", "UCzhangxi"),), repos=(Repo("o/r", "driver", ("UCzhangxi",)),))
    for rest, merges in (([REAL_REVIEW], True), ([], False)):  # the same approval, once in REST and once only in latestReviews
        gh = FakeGh((PR, {"headRefOid": real_head}))
        real = gh.__call__

        def run(argv, stdin, rest=rest, real=real):
            if argv[:3] == ["gh", "pr", "view"]:
                real(argv, stdin)
                return json.dumps({**REAL_VIEW, "state": "OPEN", "mergedAt": None, "mergeCommit": None, "author": {"login": "implementer"}, "milestone": {"title": "R1"}})
            if argv[:3] == ["gh", "api", "--paginate"]:
                real(argv, stdin)
                return json.dumps(rest)
            return real(argv, stdin)
        g, _ = make(run, cfg)
        w = World(cfg=cfg)
        w.to_implement()
        w.finish("implementer", result(artifacts=[PR], machine_state={"branch": "b", "commit": real_head[:7], "dirty": False}))
        out = poll(g, w)
        assert gh.argv("gh", "api", "--paginate") == [["gh", "api", "--paginate", "repos/o/r/pulls/9/reviews?per_page=100"]]
        if merges:
            assert [e.data for e in out.events] == [{"sender": REV, "pr": PR, "sha": real_head, "verdict": "approve"}] and w.state.audit_scopes["scope"]["verdict"] == "approve"
            assert gh.argv("gh", "pr", "merge") == [["gh", "pr", "merge", "9", "-R", "o/r", "--squash", "--match-head-commit", real_head]]
            assert (f"review-{REAL_REVIEW['id']}", f"SIGN-OFF {contracts.MIRROR_MARK} UCzhangxi: APPROVED on o/r#9 at {real_head[:12]}") in out.posts
        else:
            assert out.events == [] and not gh.argv("gh", "pr", "merge") and w.state.audit_scopes["scope"]["signed_at"] is None
        assert all("latestReviews" not in a[a.index("--json") + 1] for a in gh.argv("gh", "pr", "view"))


def test_b1_latest_review_per_login_is_the_greatest_submitted_at_then_id():
    gh = FakeGh((PR, {"reviews": [rv(5, "reviewer", "APPROVED", at="2026-10-04T10:00:00Z"), rv(3, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T11:00:00Z")]}))
    g, _ = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert [e.data["verdict"] for e in out.events if e.kind == "sign_off"] == ["changes"] and not gh.argv("gh", "pr", "merge")
    same = "2026-10-04T10:00:00Z"
    gh = FakeGh((PR, {"reviews": [rv(8, "reviewer", "APPROVED", at=same), rv(7, "reviewer", "CHANGES_REQUESTED", at=same), rv(9, "reviewer", "COMMENTED", at="2026-10-04T12:00:00Z")]}))
    g, _ = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert [e.data["verdict"] for e in out.events if e.kind == "sign_off"] == ["approve"] and len(gh.argv("gh", "pr", "merge")) == 1  # a later COMMENTED keeps the approval


def test_b2_only_the_assigned_auditor_counts_never_an_outsider_or_the_author():
    gh = FakeGh((PR, {"author": {"login": "rev2"}, "reviews": [rv(1, "stranger", "APPROVED", body="lgtm"), rv(2, "rev2", "APPROVED")]}))
    g, _ = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert not gh.argv("gh", "pr", "merge") and [e.kind for e in out.events] == ["finding", "finding"] and out.posts == []
    assert w.state.findings[-2].endswith(f"GitHub review by stranger on o/r#9 at {HEAD[:12]} (APPROVED) not counted: not the assigned auditor of o/r#9: lgtm")
    assert f"GitHub review by rev2 on o/r#9 at {HEAD[:12]} (APPROVED) not counted: the PR's author" in w.state.findings[-1]
    assert w.state.audit_scopes["scope"]["signed_at"] is None and w.state.stage == "Audit"
    gh.prs[("o/r", "9")]["reviews"].append(rv(3, "reviewer", "APPROVED"))
    out = poll(g, w)
    assert len(gh.argv("gh", "pr", "merge")) == 1 and out.posts[-1][1].endswith(f"after approval by reviewer on {HEAD[:12]}")


def test_b3_a_delivery_that_raises_is_redelivered_on_the_next_poll():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "COMMENTED", body="nit")]}))
    g, _ = make(gh)
    w = audit_world()
    calls: list[str] = []

    def deliver(item):
        calls.append(item.key)
        if item.events and calls.count(item.key) == 1: raise RuntimeError("store busy")
        for ev in item.events: w.feed(ev)
    with pytest.raises(RuntimeError): g.poll(w.state, w.now, deliver)
    assert not any("COMMENTED" in f for f in w.state.findings)
    g.poll(w.state, w.now, deliver)
    g.poll(w.state, w.now, deliver)
    assert calls == ["post:review-1", "1", "1"] and sum("COMMENTED" in f for f in w.state.findings) == 1  # the post once, the events again


def test_b3_driver_redelivers_a_post_or_event_that_failed(tmp_path, sock_dir):
    sock = os.path.join(sock_dir, "c.sock")
    server = FakeControl(sock, owner=OWNER).start()
    try:
        cfg = dataclasses.replace(GCFG, socket=sock, state_path=str(tmp_path / "s.sqlite3"))
        w = audit_world(cfg)
        store = Store(cfg.state_file)
        store.save(w.state, 0, w.now)
        gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "COMMENTED", body="looks fine")]}))
        g, _ = make(gh, cfg)
        drv = Driver(cfg, Client(cfg.socket_path), store, llm=lambda n, p: LLM[n], clock=lambda: w.now, sleep=lambda s: None, github=g)
        post, apply, fails = drv.client.post_message, drv.apply, {"post": 1, "apply": 1}

        def flaky_post(*a, **kw):
            if contracts.MIRROR_MARK in a[1].text and fails["post"]:
                fails["post"] -= 1
                raise ControlError(503, "busy")
            return post(*a, **kw)

        def flaky_apply(thread, ev, cursor=None):
            if ev.kind == "finding" and "COMMENTED" in ev["text"] and fails["apply"]:
                fails["apply"] -= 1
                raise RuntimeError("disk full")
            return apply(thread, ev, cursor)
        drv.client.post_message, drv.apply = flaky_post, flaky_apply
        for _ in range(4): drv.run_once()
        assert fails == {"post": 0, "apply": 0}
        assert len([m for m in server.view(w.state.thread)["messages"] if contracts.MIRROR_MARK in m["text"]]) == 1
        assert sum("COMMENTED" in f for f in store.load(w.state.thread).findings) == 1
    finally:
        server.stop()


def test_b4_open_pr_resumes_after_a_failed_step_without_a_second_pr():
    gh = FakeGh()
    g, meta = make(gh)
    body = g.next_pr_body("o/r", "s", 21, "T", 2)
    gh.fail_once(lambda a: a[:4] == ["gh", "api", "-X", "PATCH"])
    with pytest.raises(RuntimeError): g.open_pr("o/r", "b", "main", "t", body)
    gh.fail_once(lambda a: "repos/o/r/pulls/13/requested_reviewers" in a)
    with pytest.raises(RuntimeError, match="incomplete"): g.open_pr("o/r", "b", "main", "t", body)
    assert g.open_pr("o/r", "b", "main", "t", body) == "https://github.com/o/r/pull/13"
    assert len(gh.argv("gh", "pr", "create")) == 1 and len(gh.argv("gh", "api", "-X", "PATCH")) == 2
    assert [d["variables"]["content"] for a, d in gh.calls if d and d.get("query") == board.M_ADD_ITEM] == ["PR_r_13"]
    assert [d["reviewers"] for a, d in gh.api("repos/o/r/pulls/13/requested_reviewers")] == [["reviewer"], ["rev2"], ["reviewer"]]
    fresh, _ = make(gh)  # the record lost: the PR of the earlier attempt is found by its head branch
    assert fresh.open_pr("o/r", "b", "main", "t", body) == "https://github.com/o/r/pull/13" and len(gh.argv("gh", "pr", "create")) == 1
    gh = FakeGh((PR, {}))  # the PR under audit: a failed milestone is retried without adding the PR to the project again
    g, _ = make(gh)
    w = audit_world()
    gh.fail_once(lambda a: a[:4] == ["gh", "api", "-X", "PATCH"])
    poll(g, w)
    poll(g, w)
    poll(g, w)
    assert [d["variables"]["content"] for a, d in gh.calls if d and d.get("query") == board.M_ADD_ITEM] == ["PR_r_9"] and len(gh.argv("gh", "api", "-X", "PATCH")) == 2


def test_round4_1_only_the_assigned_auditor_is_requested_and_merges():
    """#27: the PR's one assigned auditor (the study's peer audit reviewer) is its only reviewer; a configured reviewer's approval is a finding."""
    gh = FakeGh((PR, {"reviews": [rv(1, "rev2", "APPROVED", body="lgtm")]}))
    g, meta = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert [d["reviewers"] for a, d in gh.api("repos/o/r/pulls/9/requested_reviewers")] == [["reviewer"]]  # never rev2
    assert not gh.argv("gh", "pr", "merge") and out.posts == [] and [e.kind for e in out.events] == ["finding"]
    assert w.state.findings[-1].endswith(f"GitHub review by rev2 on o/r#9 at {HEAD[:12]} (APPROVED) not counted: not the assigned auditor of o/r#9: lgtm")
    assert w.state.audit_scopes["scope"]["signed_at"] is None and w.state.stage == "Audit"
    gh.prs[("o/r", "9")]["reviews"].append(rv(2, "reviewer", "APPROVED", at="2026-10-04T11:00:00Z"))
    out = poll(g, w)
    assert len(gh.argv("gh", "pr", "merge")) == 1 and out.posts[-1][1] == f"merged o/r#9 (squash) as {SQUASH} after approval by reviewer on {HEAD[:12]}"
    two = dataclasses.replace(GCFG, reviewers=(Reviewer(REV, "scope", "reviewer"), Reviewer("UREV2", "code", "rev2")), audit_scopes=("scope", "code"))
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED")]}))  # two peer reviewers: no single assigned auditor, no request, no merge
    g, _ = make(gh, two)
    w = audit_world(two)
    poll(g, w)
    poll(g, w)
    assert not gh.api("repos/o/r/pulls/9/requested_reviewers") and not gh.argv("gh", "pr", "merge")
    assert sum("has no single assigned auditor" in f for f in w.state.findings) == 1


def test_l2_a_merge_whose_follow_up_view_fails_is_still_recorded():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED")]}))
    g, meta = make(gh)
    w = audit_world()
    gh.fail_once(lambda a: a[:3] == ["gh", "pr", "view"] and bool(gh.argv("gh", "pr", "merge")))
    poll(g, w)
    assert len(gh.argv("gh", "pr", "merge")) == 1 and "github:revision:o/r:g1" not in meta
    out = poll(g, w)
    assert meta["github:revision:o/r:g1"] == SQUASH and out.posts == [(f"merged-{HEAD[:12]}", f"merged o/r#9 (squash) as {SQUASH} after approval by reviewer on {HEAD[:12]}")]
    assert poll(g, w).posts == [] and len(gh.argv("gh", "pr", "merge")) == 1


def test_l3_study_board_and_milestone_on_one_line():
    line = "Study: Slack study thread 1759581234.567890, board https://github.com/users/chengcli/projects/8, milestone R2"
    body = f"Summary.\n\nCloses #21\n{line}\n"
    assert contracts.pr_hygiene_missing(body) == [] and contracts.pr_milestone(body) == "R2"
    assert contracts.pr_hygiene_missing(f"Summary.\n\nCloses #21\n{line.replace(', milestone R2', '')}\n") == ["milestone"]
    g, _ = make(gh := FakeGh())
    assert g.open_pr("o/r", "b", "main", "t", body) == "https://github.com/o/r/pull/13" and gh.api("repos/o/r/milestones")[0][1] == {"title": "R2"}


def test_l4_a_reviewer_github_refuses_is_requested_once_and_recorded():
    gh = FakeGh((PR, {}))
    gh.refuse = {"reviewer"}
    g, _ = make(gh)
    w = audit_world()
    poll(g, w)
    poll(g, w)
    reqs = lambda: [d["reviewers"] for a, d in gh.api("repos/o/r/pulls/9/requested_reviewers")]  # noqa: E731
    assert reqs() == [["reviewer"]]
    assert sum("GitHub refused the review request for reviewer on o/r#9" in f for f in w.state.findings) == 1
    gh.prs[("o/r", "9")]["headRefOid"] = OLD  # a new head: the refused login is not requested again
    poll(g, w)
    assert reqs() == [["reviewer"]]
    gh = FakeGh((PR, {}))
    g, _ = make(gh)
    w = audit_world()
    poll(g, w)
    gh.prs[("o/r", "9")]["headRefOid"] = OLD  # a new head: requested again, a transient failure retried
    gh.fail_once(lambda a: "repos/o/r/pulls/9/requested_reviewers" in a)
    poll(g, w)
    poll(g, w)
    poll(g, w)
    assert reqs() == [["reviewer"], ["reviewer"], ["reviewer"]]


# -- R24: late reviews -------------------------------------------------------------------
def test_post_merge_changes_requested_is_acknowledged_recorded_and_carried():
    late = rv(9, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T13:00:00Z", body="- rename foo\n- add a test for bar\n")
    gh = FakeGh((PR, {"state": "MERGED", "mergedAt": MERGED_AT, "mergeCommit": {"oid": SQUASH}, "reviews": [rv(1, "rev2", "APPROVED", at="2026-10-04T11:00:00Z")]}))
    g, meta = make(gh)
    w = audit_world()
    w.ev("sign_off", sender=REV, pr=PR, sha=SHA, verdict="approve")  # the study delivered on the Slack fallback
    assert w.state.stage == "Delivered"
    poll(g, w)
    gh.prs[("o/r", "9")]["reviews"].append(late)
    out = poll(g, w)
    assert gh.api("repos/o/r/issues/9/comments") == [(["gh", "api", "-X", "POST", "repos/o/r/issues/9/comments", "--input", "-"], {"body": "@reviewer acknowledged, goes into the next PR."})]
    assert ("ack-9", "acknowledged, goes into the next PR: post-merge review by reviewer on o/r#9") in out.posts
    assert any(t.endswith("(after merge)") for _, t in out.posts)
    assert w.state.findings[-2:] == ["iteration 1 note during Delivered: post-merge review by reviewer on o/r#9: rename foo", "iteration 1 note during Delivered: post-merge review by reviewer on o/r#9: add a test for bar"]
    assert w.state.stage == "Delivered" and not gh.argv("gh", "pr", "merge")
    body = g.next_pr_body("o/r", "Next change.", 21, w.state.thread, 2)
    assert "## From post-merge review by reviewer\n- rename foo\n- add a test for bar" in body
    poll(g, w)
    assert len(gh.api("repos/o/r/issues/9/comments")) == 1  # one reply per review
    g.open_pr("o/r", "next", "main", "Next", body)
    assert "github:carry:o/r" not in meta  # carried into the PR that was opened



def test_round2_b1_a_failed_acknowledgement_is_retried_with_each_effect_once():
    late = rv(9, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T13:00:00Z", body="- rename foo\n")
    gh = FakeGh((PR, {"state": "MERGED", "mergedAt": MERGED_AT, "mergeCommit": {"oid": SQUASH}, "reviews": [late]}))
    g, _ = make(gh)
    w = audit_world()
    w.ev("sign_off", sender=REV, pr=PR, sha=SHA, verdict="approve")
    gh.fail_once(lambda a: "repos/o/r/issues/9/comments" in a)
    out = poll(g, w)
    assert not any(k.startswith("post:ack") for k in (i.key for i in out.items)) and "9" not in [i.key for i in out.items]
    out = poll(g, w)
    poll(g, w)
    assert len(gh.api("repos/o/r/issues/9/comments")) == 2  # the failed reply, then the retry; none after
    assert ("ack-9", "acknowledged, goes into the next PR: post-merge review by reviewer on o/r#9") in out.posts
    assert sum(f.endswith("post-merge review by reviewer on o/r#9: rename foo") for f in w.state.findings) == 1
    assert g.carried("o/r") == {"reviewer": ["rename foo"]}


def test_round2_b2_an_outsiders_changes_request_does_not_veto_a_configured_approval():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED"), rv(2, "stranger", "CHANGES_REQUESTED", body="no")]}))
    g, _ = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert len(gh.argv("gh", "pr", "merge")) == 1 and out.posts[-1][1].endswith(f"after approval by reviewer on {HEAD[:12]}")
    assert any(f.endswith(f"GitHub review by stranger on o/r#9 at {HEAD[:12]} (CHANGES_REQUESTED) not counted: not the assigned auditor of o/r#9: no") for f in w.state.findings)


def test_round2_f3_a_dismissed_approval_is_taken_back_and_never_counts():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED")]}))
    g, _ = make(gh)
    w = audit_world()
    gh.fail_once(lambda a: a[:3] == ["gh", "pr", "merge"])  # the first merge fails; the PR stays open
    poll(g, w)
    assert len(gh.argv("gh", "pr", "merge")) == 1 and gh.prs[("o/r", "9")]["state"] == "OPEN"
    rs = gh.prs[("o/r", "9")]["reviews"]
    rs[0]["state"] = "DISMISSED"  # GitHub shows a dismissed review with its id, commit and submitted_at unchanged
    out = poll(g, w)
    assert ("dismissed-1", f"SIGN-OFF (GitHub review, mirrored) reviewer: DISMISSED on o/r#9 at {HEAD[:12]}") in out.posts and len(gh.argv("gh", "pr", "merge")) == 1
    assert w.state.findings[-1].endswith(f"GitHub review by reviewer on o/r#9 at {HEAD[:12]} was dismissed: it no longer counts toward the merge")
    poll(g, w)
    assert len(gh.argv("gh", "pr", "merge")) == 1  # a dismissed verdict never merges
    rs.append(rv(3, "reviewer", "APPROVED", at="2026-10-04T11:00:00Z"))
    out = poll(g, w)
    assert len(gh.argv("gh", "pr", "merge")) == 2 and out.posts[-1][1].endswith(f"after approval by reviewer on {HEAD[:12]}")
    assert sum("was dismissed" in f for f in w.state.findings) == 1  # the dismissal once


def test_round2_f2_post_merge_review_items_are_capped():
    from fridica_research.github import review_items
    assert review_items("- " + "x" * 2000 + "\nshort") == ["x" * 500, "short"]

def test_merged_pr_stops_polling_after_the_window():
    gh = FakeGh((PR, {"state": "MERGED", "mergedAt": MERGED_AT, "mergeCommit": {"oid": SQUASH}}))
    g, _ = make(gh, dataclasses.replace(GCFG, github=GitHubCfg(enabled=True, poll_interval=0, post_merge_window=60)))
    w = audit_world()
    end = parse_iso(MERGED_AT) + 61
    g.poll(w.state, end - 120)
    g.poll(w.state, end)
    g.poll(w.state, end + 10)
    assert len(gh.argv("gh", "pr", "view")) == 2  # inside the window, then the poll that finds it over; none after



def test_round3_b1_a_failed_acknowledgement_after_the_window_is_retried_with_each_effect_once():
    late = rv(9, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T13:00:00Z", body="- rename foo\n")
    gh = FakeGh((PR, {"state": "MERGED", "mergedAt": MERGED_AT, "mergeCommit": {"oid": SQUASH}, "reviews": [late]}))
    g, _ = make(gh, dataclasses.replace(GCFG, github=GitHubCfg(enabled=True, poll_interval=0, post_merge_window=7200)))
    w = audit_world()
    w.ev("sign_off", sender=REV, pr=PR, sha=SHA, verdict="approve")
    end = parse_iso(MERGED_AT) + 7200 + 1  # the first poll comes after the post-merge window closed
    gh.fail_once(lambda a: "repos/o/r/issues/9/comments" in a)
    out = poll(g, w, end)
    assert "9" not in [i.key for i in out.items]
    out = poll(g, w, end + 10)
    poll(g, w, end + 20)
    assert len(gh.api("repos/o/r/issues/9/comments")) == 2  # the failed reply, then the retry; none after
    assert ("ack-9", "acknowledged, goes into the next PR: post-merge review by reviewer on o/r#9") in out.posts
    assert sum(f.endswith("post-merge review by reviewer on o/r#9: rename foo") for f in w.state.findings) == 1
    assert g.carried("o/r") == {"reviewer": ["rename foo"]}
    assert len(gh.argv("gh", "pr", "view")) == 2  # done once the acknowledgement was delivered; no poll after


def test_round3_a_delivery_that_raises_after_the_window_is_redelivered():
    late = rv(9, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T13:00:00Z", body="- rename foo\n")
    gh = FakeGh((PR, {"state": "MERGED", "mergedAt": MERGED_AT, "mergeCommit": {"oid": SQUASH}, "reviews": [late]}))
    g, _ = make(gh, dataclasses.replace(GCFG, github=GitHubCfg(enabled=True, poll_interval=0, post_merge_window=7200)))
    w = audit_world()
    w.ev("sign_off", sender=REV, pr=PR, sha=SHA, verdict="approve")
    end = parse_iso(MERGED_AT) + 7200 + 1
    calls: list[str] = []

    def deliver(item):
        calls.append(item.key)
        if item.key == "9" and calls.count("9") == 1: raise RuntimeError("store busy")
        for ev in item.events: w.feed(ev)
    with pytest.raises(RuntimeError): g.poll(w.state, end, deliver)
    g.poll(w.state, end + 10, deliver)
    g.poll(w.state, end + 20, deliver)
    assert calls.count("9") == 2 and calls.count("post:ack-9") == 1 and len(gh.api("repos/o/r/issues/9/comments")) == 1
    assert sum(f.endswith("post-merge review by reviewer on o/r#9: rename foo") for f in w.state.findings) == 1


def test_round3_a_closed_prs_findings_are_delivered_before_it_is_done():
    gh = FakeGh((PR, {"state": "CLOSED", "reviews": [rv(1, "reviewer", "COMMENTED", body="nit")]}))
    g, _ = make(gh)
    w = audit_world()
    calls: list[str] = []

    def deliver(item):
        calls.append(item.key)
        if item.key == "1" and calls.count("1") == 1: raise RuntimeError("store busy")
        for ev in item.events: w.feed(ev)
    with pytest.raises(RuntimeError): g.poll(w.state, w.now, deliver)
    g.poll(w.state, w.now, deliver)
    g.poll(w.state, w.now, deliver)
    assert calls.count("1") == 2 and sum("COMMENTED" in f for f in w.state.findings) == 1 and len(gh.argv("gh", "pr", "view")) == 2

# -- pr_id and the short-sha guard ---------------------------------------------------------
def test_two_prs_12_in_two_repos():
    a, b = "https://github.com/a/x/pull/12", "https://github.com/b/y/pull/12"
    assert contracts.pr_id(a) == ("a/x", "12") and contracts.pr_id("b/y#12") == ("b/y", "12") and contracts.pr_id("#12") == ("", "12")
    assert not contracts.same_pr(a, b) and not machine.head_matches(b, SHA, a, SHA) and machine.head_matches("#12", SHA, a, SHA)
    w = audit_world(pr=a)
    w.ev("sign_off", sender=REV, pr=b, sha=SHA, verdict="approve")
    assert w.state.stage == "Audit" and "sign-off from UREV ignored" in w.state.findings[-1]
    gh = FakeGh((a, {}), (b, {"reviews": [rv(1, "reviewer", "APPROVED")]}))
    g, meta = make(gh, dataclasses.replace(GCFG, repos=(Repo("a/x", "driver"), Repo("b/y", "driver"))))
    out = poll(g, w)
    assert out.events == [] and w.state.stage == "Audit" and not gh.argv("gh", "pr", "merge")  # b/y#12's approval is not a/x#12's
    assert gh.argv("gh", "pr", "view")[0][3:6] == ["12", "-R", "a/x"] and f"github:pr:{w.state.thread}:a/x#12" in meta


def test_head_matches_short_sha_guard():
    assert machine.head_matches(PR, HEAD, PR, SHA) and machine.head_matches(PR, SHA, PR, HEAD)  # either side abbreviated
    assert not machine.head_matches(PR, HEAD[:6], PR, HEAD) and not machine.head_matches(PR, "abc12", PR, "abc12")  # under 7 digits names no head
    assert not machine.head_matches(PR, OLD, PR, SHA)


# -- R23: PR hygiene ------------------------------------------------------------------------
def test_refuses_to_open_a_pr_without_closes_study_board_milestone():
    g, _ = make(gh := FakeGh())
    good = g.next_pr_body("o/r", "Summary.", 21, "T1:C1:1.2", 2)
    assert "| driver and arbitrator |" in good and "| explorer |" in good and "| auditor |" in good
    assert contracts.pr_hygiene_missing(good) == [] and "Board: https://github.com/users/chengcli/projects/8" in good and "Milestone: R2" in good
    for line, name in (("Closes #21", "Closes #N"), ("Study thread: T1:C1:1.2", "study thread"), ("Board: https://github.com/users/chengcli/projects/8", "board link"), ("Milestone: R2", "milestone")):
        with pytest.raises(PrHygieneError, match=name): g.open_pr("o/r", "b", "main", "t", good.replace(line, ""))
    with pytest.raises(PrHygieneError, match="Closes #N"): g.open_pr("o/r", "b", "main", "t", g.next_pr_body("o/r", "s", None, "T", 2))
    no_board, _ = make(gh, dataclasses.replace(GCFG, board=BoardCfg()))
    with pytest.raises(PrHygieneError, match="board link"): no_board.open_pr("o/r", "b", "main", "t", no_board.next_pr_body("o/r", "s", 21, "T", 2))
    assert not gh.argv("gh", "pr", "create")
    url = g.open_pr("o/r", "b", "main", "t", good)
    assert url == "https://github.com/o/r/pull/13" and gh.argv("gh", "pr", "create")[0][-1] == good
    assert [d["variables"]["content"] for a, d in gh.calls if d and d.get("query") == board.M_ADD_ITEM] == ["PR_r_13"]
    assert ["gh", "api", "-X", "PATCH", "repos/o/r/issues/13", "-F", "milestone=1"] in gh.argv("gh", "api", "-X", "PATCH")
    assert gh.api("repos/o/r/milestones")[0][1] == {"title": "R2"}  # created on demand


def test_pr_under_audit_is_put_on_the_project_and_milestone_once():
    gh = FakeGh((PR, {}))
    g, _ = make(gh)
    w = audit_world()
    poll(g, w)
    poll(g, w)
    assert [d["variables"]["content"] for a, d in gh.calls if d and d.get("query") == board.M_ADD_ITEM] == ["PR_r_9"]
    assert gh.argv("gh", "api", "-X", "PATCH") == [["gh", "api", "-X", "PATCH", "repos/o/r/issues/9", "-F", "milestone=1"]]


# -- config ----------------------------------------------------------------------------------
def test_github_and_repos_config():
    c = config.parse('[github]\nenabled = true\npoll_interval = "5m"\npost_merge_window = "2d"\n[repos]\n"chengcli/fridica-research" = {merge = "driver", reviewers = ["alice"]}\n"chengcli/fridica" = {merge = "owner"}\n[people]\nU1 = "Alice"\n')
    assert c.github.enabled and c.github.poll_interval == 300 and c.github.post_merge_window == 172800 and "copilot" in c.github.bots
    assert c.repo("ChengCLI/fridica-research") == Repo("chengcli/fridica-research", "driver", ("alice",)) and c.repo("chengcli/fridica").merge == "owner" and c.repo("x/y").merge == "owner"
    assert c.slack_of("alice") == "U1" and c.slack_of("bob", {"U2": "bob"}) == "U2" and c.slack_of("nobody") == ""
    assert config.Config.from_dict(json.loads(json.dumps(c.to_dict()))) == c
    assert not config.parse("").github.enabled and config.parse("").repos == ()
    with pytest.raises(ValueError, match="merge"): config.parse('[repos]\n"o/r" = {merge = "anyone"}\n')


# -- round 4 of review ------------------------------------------------------------------------
def test_round4_3_a_late_item_added_after_the_body_was_prepared_stays_carried_to_the_next_pr():
    gh = FakeGh((PR, {"state": "MERGED", "mergedAt": MERGED_AT, "mergeCommit": {"oid": SQUASH}, "reviews": [rv(9, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T13:00:00Z", body="- rename foo\n")]}))
    g, meta = make(gh)
    w = audit_world()
    w.ev("sign_off", sender=REV, pr=PR, sha=SHA, verdict="approve")
    poll(g, w)
    body = g.next_pr_body("o/r", "Next change.", 21, w.state.thread, 2)
    gh.prs[("o/r", "9")]["reviews"].append(rv(10, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T14:00:00Z", body="- add a test for bar\n"))
    poll(g, w)  # the second late review lands after `body` was prepared
    assert g.carried("o/r") == {"reviewer": ["rename foo", "add a test for bar"]}
    g.open_pr("o/r", "next", "main", "Next", body)
    assert g.carried("o/r") == {"reviewer": ["add a test for bar"]}  # only what the opened PR's body lists leaves the carry
    g.open_pr("o/r", "next", "main", "Next", g.next_pr_body("o/r", "Retry.", 21, w.state.thread, 2))  # a retry for the same PR clears nothing more
    assert g.carried("o/r") == {"reviewer": ["add a test for bar"]}
    body2 = g.next_pr_body("o/r", "After.", 21, w.state.thread, 2)
    assert "- add a test for bar" in body2 and "rename foo" not in body2
    g.open_pr("o/r", "after", "main", "After", body2)
    assert "github:carry:o/r" not in meta  # each item written into exactly one PR body


def test_round4_4_a_draft_pr_is_never_merged_and_noted_once_per_head():
    gh = FakeGh((PR, {"isDraft": True, "reviews": [rv(1, "reviewer", "APPROVED")]}))
    g, _ = make(gh)
    w = audit_world()
    for _ in range(3): poll(g, w)
    assert not gh.argv("gh", "pr", "merge")
    assert sum(f.endswith(f"o/r#9 is approved on {HEAD[:12]} but is a draft: the driver merges it once it is marked ready for review") for f in w.state.findings) == 1
    assert all("isDraft" in a[a.index("--json") + 1] for a in gh.argv("gh", "pr", "view"))
    gh.prs[("o/r", "9")]["isDraft"] = False  # marked ready for review
    poll(g, w)
    assert gh.argv("gh", "pr", "merge") == [["gh", "pr", "merge", "9", "-R", "o/r", "--squash", "--match-head-commit", HEAD]]


def test_round4_5_a_merge_that_errors_after_github_merged_records_the_squash_sha():
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED")]}))

    def run(argv, stdin):
        out = gh(argv, stdin)
        if argv[:3] == ["gh", "pr", "merge"]: raise RuntimeError("gh pr merge failed: HTTP 502 (the merge went through)")
        return out
    g, meta = make(run)
    w = audit_world()
    out = poll(g, w)
    assert len(gh.argv("gh", "pr", "merge")) == 1 and meta["github:revision:o/r:g1"] == SQUASH
    assert out.posts[-1] == (f"merged-{HEAD[:12]}", f"merged o/r#9 (squash) as {SQUASH} after approval by reviewer on {HEAD[:12]}")
    assert poll(g, w).posts == [] and len(gh.argv("gh", "pr", "merge")) == 1  # once


# -- round 5: an approval that stops counting reopens the audit; every late review is its own --------------------------
def held_world(cfg=GCFG) -> World:
    """An audit world whose deliver call is left unanswered, so an approval leaves the study in Deliver."""
    w = World(cfg=cfg)
    react = w.react

    def hold_deliver(actions):
        held = [a for a in actions if a.kind == "llm_call" and a["name"] == "study_deliver"]
        react([a for a in actions if a not in held])
        w.actions.extend(held)
    w.react = hold_deliver
    w.to_audit()
    return w


def approved_in_deliver(cfg=GCFG):
    """The auditor's APPROVED review is delivered (scope approved, study in Deliver) and the first merge fails, so the PR stays open."""
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED")]}))
    g, meta = make(gh, cfg)
    w = held_world(cfg)
    gh.fail_once(lambda a: a[:3] == ["gh", "pr", "merge"])
    poll(g, w)
    assert w.state.stage == "Deliver" and w.state.audit_scopes["scope"]["verdict"] == "approve" and len(gh.argv("gh", "pr", "merge")) == 1
    return gh, g, w


def assert_reopened(w: World, gh: FakeGh, merges: int = 1):
    assert w.state.stage == "Explore" and w.state.iteration == 2 and w.state.audit["verdict"] == "return"
    assert any("peer review asked for changes: scope=dismissed" in f for f in w.state.findings)
    assert len(gh.argv("gh", "pr", "merge")) == merges  # the withdrawn approval never merges


def test_round5_a_a_dismissed_approval_reopens_its_audit_scope():
    gh, g, w = approved_in_deliver()
    gh.prs[("o/r", "9")]["reviews"][0]["state"] = "DISMISSED"  # dismissed by hand: id, commit and submitted_at unchanged
    out = poll(g, w)
    assert ("dismissed-1", f"SIGN-OFF (GitHub review, mirrored) reviewer: DISMISSED on o/r#9 at {HEAD[:12]}") in out.posts
    assert [e.data for e in out.events if e.kind == "sign_off"] == [{"sender": REV, "pr": PR, "sha": HEAD, "verdict": "dismissed"}]
    assert_reopened(w, gh)
    out = poll(g, w)
    assert out.items == [] and sum("was dismissed" in f for f in w.state.findings) == 1  # withdrawn once


@pytest.mark.parametrize("dismissed", [True, False])
def test_round5_a_a_push_withdraws_the_approval_of_the_old_head(dismissed):
    """Branch protection dismisses a stale approval on push (its commit_id stays the old head); without that rule it only stops counting."""
    gh, g, w = approved_in_deliver()
    pr = gh.prs[("o/r", "9")]
    pr["headRefOid"] = "abcdef1" + "2" * 33
    if dismissed: pr["reviews"][0]["state"] = "DISMISSED"
    out = poll(g, w)
    assert (("dismissed-1", f"SIGN-OFF (GitHub review, mirrored) reviewer: DISMISSED on o/r#9 at {HEAD[:12]}") in out.posts) == dismissed
    assert any(e.kind == "sign_off" and e.data["verdict"] == "dismissed" and e.data["sha"] == HEAD for e in out.events)
    assert_reopened(w, gh)


def test_round5_a_an_approval_superseded_by_a_dismissed_changes_request_is_withdrawn():
    gh, g, w = approved_in_deliver()
    gh.prs[("o/r", "9")]["reviews"].append(rv(2, "reviewer", "DISMISSED", at="2026-10-04T11:00:00Z", body="wait"))  # a changes request dismissed before the poll
    out = poll(g, w)
    assert any(f.endswith("no longer counts (superseded by a later DISMISSED review): it is withdrawn from the audit") for f in w.state.findings) and out.events
    assert_reopened(w, gh)


def test_round5_a_an_approval_by_a_login_that_is_no_longer_the_auditor_is_withdrawn():
    cfg = dataclasses.replace(GCFG, reviewers=(Reviewer(REV, "scope"),), people={OWNER: "chengcli"})  # the auditor's login is learned in the thread
    gh = FakeGh((PR, {"reviews": [rv(1, "reviewer", "APPROVED")]}))
    g, _ = make(gh, cfg)
    w = held_world(cfg)
    w.ev("login_reply", sender=REV, login="reviewer")
    gh.fail_once(lambda a: a[:3] == ["gh", "pr", "merge"])
    poll(g, w)
    assert w.state.stage == "Deliver" and w.state.audit_scopes["scope"]["verdict"] == "approve"
    w.ev("login_reply", sender=REV, login="reviewer-new")
    poll(g, w)
    assert any(f.endswith("no longer counts (not the assigned auditor of o/r#9): it is withdrawn from the audit") for f in w.state.findings)
    assert_reopened(w, gh)


def test_round5_b_every_post_merge_changes_review_is_acknowledged_and_carried_once():
    gh = FakeGh((PR, {"state": "MERGED", "mergedAt": MERGED_AT, "mergeCommit": {"oid": SQUASH}}))
    g, _ = make(gh)
    w = audit_world()
    w.ev("sign_off", sender=REV, pr=PR, sha=SHA, verdict="approve")
    poll(g, w)
    rs = gh.prs[("o/r", "9")]["reviews"]
    rs += [rv(9, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T13:00:00Z", body="- rename foo"), rv(10, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T14:00:00Z", body="- add a test")]
    out = poll(g, w)  # two late changes requests in one poll
    poll(g, w)
    ack = "acknowledged, goes into the next PR: post-merge review by reviewer on o/r#9"
    assert [d["body"] for _, d in gh.api("repos/o/r/issues/9/comments")] == ["@reviewer acknowledged, goes into the next PR."] * 2
    assert [p for p in out.posts if p[0].startswith("ack-")] == [("ack-9", ack), ("ack-10", ack)] and [k for k, _ in out.posts if k.startswith("review-")] == ["review-9", "review-10"]
    assert g.carried("o/r") == {"reviewer": ["rename foo", "add a test"]}
    assert [f.rsplit(": ", 1)[1] for f in w.state.findings if "post-merge review by reviewer" in f] == ["rename foo", "add a test"]
    rs += [rv(12, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T15:00:00Z", body="- fix baz"), rv(13, "reviewer", "APPROVED", at="2026-10-04T16:00:00Z")]
    out = poll(g, w)  # a late approval in the same poll does not swallow the changes request before it
    assert ("ack-12", ack) in out.posts and len(gh.api("repos/o/r/issues/9/comments")) == 3 and g.carried("o/r")["reviewer"][-1] == "fix baz"


def test_round5_b_reviews_of_one_login_in_one_poll_each_keep_their_mirror_line_or_finding():
    gh = FakeGh((PR, {"reviews": [rv(5, "reviewer", "APPROVED", at="2026-10-04T10:00:00Z"), rv(6, "reviewer", "CHANGES_REQUESTED", at="2026-10-04T11:00:00Z"),
                                  rv(7, "stranger", "APPROVED", body="one"), rv(8, "stranger", "CHANGES_REQUESTED", at="2026-10-04T11:00:00Z", body="two")]}))
    g, _ = make(gh)
    w = audit_world()
    out = poll(g, w)
    assert [k for k, _ in out.posts] == ["review-5", "review-6"] and [e.data["verdict"] for e in out.events if e.kind == "sign_off"] == ["changes"]
    assert [f.rsplit(": ", 1)[1] for f in w.state.findings if "by stranger" in f] == ["one", "two"]
    assert not gh.argv("gh", "pr", "merge")
