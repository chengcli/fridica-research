"""#39a: an adopted study issue (read live, consumed once, one comment, never closed by the driver), the PR milestone
from the adopted issue (R23), `pr --milestone` as an override, and a mirror line that never starts with SIGN-OFF."""
from __future__ import annotations

import json

from fridica_research import cli, contracts, github
from fridica_research.store import Store
import test_board as tb
import test_github as tg
from support import OWNER, REV, World


# -- the adopted study issue (board) --------------------------------------------------------
def test_issue_given_after_the_board_was_built_is_adopted_and_its_key_consumed():
    """B3: `start --issue N` writes `issue:<channel>` while `serve` runs; the board reads it at adoption, not from a startup map."""
    order: list[str] = []
    b, gh, meta = tb.make()
    remember = b.remember
    b.remember = b.api.remember = lambda k, v: (order.append(k), remember(k, v))
    w = World(cfg=tb.BCFG)
    w.start()
    meta["issue:C1"] = "42"  # written after the Board exists, as `start --issue 42` does against a running driver
    b.sync(w.state)
    cards = json.loads(meta[f"board:{w.state.thread}"])
    assert cards["issue"] == 42 and cards["given"] and [a[6] for a in gh.argv("gh", "issue", "create")] == ["Explore (iteration 1): Study the thing"]
    assert "issue:C1" not in meta and order.index(f"board:{w.state.thread}") < order.index("issue:C1")  # the cards are stored before the key goes
    w2 = World(cfg=tb.BCFG)  # a later study in the same channel creates its own issue
    w2.start(thread="T1:C1:1700000000.000999")
    b.sync(w2.state)
    assert json.loads(meta["board:T1:C1:1700000000.000999"])["given"] is False


def adopted_cards(meta: dict, thread: str, issue=42):
    """The cards of a study that adopted `issue` (as `sync_study` stores them), seeded directly so each test isolates one behaviour."""
    meta[f"board:{thread}"] = json.dumps({"issue": issue, "given": True, "filled": False, "stages": []})


def test_adopted_issue_gets_one_comment_retried_until_posted():
    b, gh, meta = tb.make()
    w = World(cfg=tb.BCFG)
    adopted_cards(meta, w.start().thread)
    gh.fail_once = "issue comment"
    b.sync(w.state)  # the comment fails: logged, retried on the next sync
    b.sync(w.state)
    b.sync(w.state)
    comments = gh.argv("gh", "issue", "comment")
    assert len(comments) == 2 and comments[-1][:5] == ["gh", "issue", "comment", "42", "-R"] and w.state.thread in comments[-1][-1]
    assert "The driver never closes this issue" in comments[-1][-1]
    assert json.loads(meta[f"board:{w.state.thread}"])["commented"] is True


def test_created_study_issue_gets_no_comment():
    b, gh, _ = tb.make()
    b.sync(World(cfg=tb.BCFG).start())
    assert gh.argv("gh", "issue", "comment") == []


def test_delivered_study_leaves_an_adopted_issue_open_but_finishes_its_card():
    """The close guard skips only `gh issue close`: Finished, Actual hours and Done are still written; a created issue still closes."""
    for given in (True, False):
        b, gh, meta = tb.make()
        w = World(cfg=tb.BCFG)
        w.start()
        if given: adopted_cards(meta, w.state.thread)
        b.sync(w.state)
        n = json.loads(meta[f"board:{w.state.thread}"])["issue"]
        w.to_delivered()
        b.sync(w.state)
        assert (["gh", "issue", "close", str(n), "-R", "chengcli/fridica-research"] in gh.argv("gh", "issue", "close")) is (not given)
        item = f"PVTI_I_{n}"
        assert any(s["item"] == item and s["field"] == "F_Finished" for s in gh.sets(tb.board.M_SET_DATE))
        assert any(s["item"] == item and s["field"] == "F_Actual_hours" for s in gh.sets(tb.board.M_SET_NUMBER))
        assert {"item": item, "v": "S_Done"}.items() <= next(s for s in gh.sets(tb.board.M_SET_OPTION)[::-1] if s["item"] == item).items()
        assert json.loads(meta[f"board:{w.state.thread}"])["closed"] is True


# -- R23: the PR milestone follows the adopted issue -------------------------------------
class IssueGh(tg.FakeGh):
    """FakeGh that also answers `gh api repos/<o>/<r>/issues/<n>` with the issue's milestone."""

    def __init__(self, *prs, issues: dict | None = None):
        super().__init__(*prs)
        self.issues = issues or {}  # number -> milestone dict or None

    def __call__(self, argv, stdin):
        if argv[:2] == ["gh", "api"] and len(argv) == 3 and "/issues/" in argv[2]:
            self.calls.append((argv, None))
            return json.dumps({"number": int(argv[2].rsplit("/", 1)[1]), "milestone": self.issues.get(int(argv[2].rsplit("/", 1)[1]))})
        return super().__call__(argv, stdin)


def adopt(meta: dict, thread: str, issue=42, given=True):
    meta[f"board:{thread}"] = json.dumps({"issue": issue, "given": given, "stages": []})


def test_pr_under_audit_takes_the_adopted_issues_milestone_number():
    gh = IssueGh((tg.PR, {}), issues={42: {"number": 7, "title": "R2"}})
    g, meta = tg.make(gh)
    w = tg.audit_world()
    adopt(meta, w.state.thread)
    tg.poll(g, w)
    tg.poll(g, w)
    assert gh.argv("gh", "api", "-X", "PATCH") == [["gh", "api", "-X", "PATCH", "repos/o/r/issues/9", "-F", "milestone=7"]]
    assert not gh.api("repos/o/r/milestones")  # by number: no title lookup, nothing created


def test_pr_under_audit_falls_back_to_the_generation_milestone():
    for given, ms in ((True, None), (False, {"number": 7, "title": "R2"})):  # adopted issue without a milestone; a study with its own issue
        gh = IssueGh((tg.PR, {}), issues={42: ms})
        g, meta = tg.make(gh)
        w = tg.audit_world()
        adopt(meta, w.state.thread, given=given)
        tg.poll(g, w)
        assert gh.argv("gh", "api", "-X", "PATCH") == [["gh", "api", "-X", "PATCH", "repos/o/r/issues/9", "-F", "milestone=1"]]
        assert gh.api("repos/o/r/milestones")[-1][1] == {"title": "R1"}  # created on demand, as before
        assert bool(gh.argv("gh", "api", "repos/o/r/issues/42")) is given  # a non-adopted study reads no issue


def write_config(tmp_path) -> str:
    p = tmp_path / "research.toml"
    p.write_text(f'state_path = "{tmp_path / "s.sqlite3"}"\n[fridica]\nowner = "{OWNER}"\n[audit]\nreviewers = [{{handle = "{REV}", scope = "scope", login = "reviewer"}}]\n'
                 '[board]\nenabled = true\nowner = "chengcli"\nnumber = 8\nrepo = "o/r"\n[github]\nenabled = true\n[repos]\n"o/r" = {merge = "driver", reviewers = ["reviewer"]}\n')
    return str(p)


def run_pr(tmp_path, monkeypatch, gh, *extra, given=True, repo="o/r"):
    monkeypatch.setattr(github, "subprocess_runner", lambda token_env: gh)
    cfgp = write_config(tmp_path)
    store = Store(tmp_path / "s.sqlite3")
    w = World()
    store.save(w.start())
    store.set_meta(f"board:{w.state.thread}", json.dumps({"issue": 42, "given": given, "stages": []}))
    store.close()
    return cli.main(["--config", cfgp, "pr", w.state.thread, "--repo", repo, "--head", "b", "--title", "t", *extra])


def test_pr_cli_takes_the_adopted_issues_milestone_number_and_names_it_when_r_n(tmp_path, monkeypatch, capsys):
    gh = IssueGh(issues={42: {"number": 5, "title": "R3"}})
    assert run_pr(tmp_path, monkeypatch, gh) == 0
    body = gh.argv("gh", "pr", "create")[0][-1]
    assert "Closes #42" in body and "Milestone: R3" in body and contracts.pr_milestone(body) == "R3"
    assert gh.argv("gh", "api", "-X", "PATCH") == [["gh", "api", "-X", "PATCH", "repos/o/r/issues/13", "-F", "milestone=5"]] and not gh.api("repos/o/r/milestones")


def test_pr_cli_names_the_generation_when_the_issues_milestone_is_not_r_n(tmp_path, monkeypatch):
    gh = IssueGh(issues={42: {"number": 4, "title": "v1.0"}})
    assert run_pr(tmp_path, monkeypatch, gh) == 0
    assert "Milestone: R1" in gh.argv("gh", "pr", "create")[0][-1]
    assert gh.argv("gh", "api", "-X", "PATCH") == [["gh", "api", "-X", "PATCH", "repos/o/r/issues/13", "-F", "milestone=4"]]


def test_pr_cli_milestone_override_never_creates(tmp_path, monkeypatch, capsys):
    gh = IssueGh(issues={42: {"number": 5, "title": "R3"}})
    gh.milestones["repos/o/r/milestones"] = [{"title": "R1", "number": 1}, {"title": "R2", "number": 2}]
    assert run_pr(tmp_path, monkeypatch, gh, "--milestone", "R9") == 1
    assert "no milestone 'R9'" in capsys.readouterr().err and not gh.argv("gh", "pr", "create") and not gh.api("repos/o/r/milestones")[1:]
    assert all("POST" not in a for a, _ in gh.api("repos/o/r/milestones"))
    assert run_pr(tmp_path, monkeypatch, gh, "--milestone", "R2") == 0
    assert "Milestone: R2" in gh.argv("gh", "pr", "create")[0][-1]
    assert gh.argv("gh", "api", "-X", "PATCH") == [["gh", "api", "-X", "PATCH", "repos/o/r/issues/13", "-F", "milestone=2"]]


def test_adopted_issue_milestone_only_for_a_pr_in_the_issues_repository(tmp_path, monkeypatch):
    """Milestone numbers are per repository: the adopted issue (in `[board] repo` o/r) gives its number only to a PR in o/r;
    a PR in another repository gets R<generation>, at open and at first poll."""
    for d in ("same", "other"): (tmp_path / d).mkdir()
    gh = IssueGh(issues={42: {"number": 5, "title": "R3"}})  # open, same repository: the issue's number and title
    assert run_pr(tmp_path / "same", monkeypatch, gh) == 0
    assert "Milestone: R3" in gh.argv("gh", "pr", "create")[0][-1]
    assert gh.argv("gh", "api", "-X", "PATCH") == [["gh", "api", "-X", "PATCH", "repos/o/r/issues/13", "-F", "milestone=5"]]
    gh = IssueGh(issues={42: {"number": 5, "title": "R3"}})  # open, another repository: the generation milestone of o/x, by title
    assert run_pr(tmp_path / "other", monkeypatch, gh, repo="o/x") == 0
    assert "Milestone: R1" in gh.argv("gh", "pr", "create")[0][-1] and not gh.argv("gh", "api", "repos/o/r/issues/42")
    assert gh.argv("gh", "api", "-X", "PATCH") == [["gh", "api", "-X", "PATCH", "repos/o/x/issues/13", "-F", "milestone=1"]] and gh.api("repos/o/x/milestones")[-1][1] == {"title": "R1"}
    for pr, patch in ((tg.PR, ["repos/o/r/issues/9", "-F", "milestone=5"]), ("https://github.com/o/x/pull/9", ["repos/o/x/issues/9", "-F", "milestone=1"])):  # first poll
        gh = IssueGh((pr, {}), issues={42: {"number": 5, "title": "R3"}})
        g, meta = tg.make(gh)
        w = tg.audit_world(pr=pr)
        adopt(meta, w.state.thread)
        tg.poll(g, w)
        assert gh.argv("gh", "api", "-X", "PATCH") == [["gh", "api", "-X", "PATCH", *patch]]


# -- Q4: the mirror line ------------------------------------------------------------------
def test_mirror_line_never_starts_with_sign_off():
    line = contracts.mirror_line("reviewer", "APPROVED", tg.PR, tg.HEAD)
    assert not line.lower().startswith("sign-off") and line.startswith(contracts.MIRROR_MARK) and contracts.parse_signoff(line) is None
    legacy = f"SIGN-OFF (GitHub review, mirrored) reviewer: APPROVED on o/r#9 at {tg.HEAD[:12]}\nSIGN-OFF {tg.PR} {tg.SHA} approve"
    assert contracts.parse_signoff(legacy) is None  # a mirror posted before the change is still never a sign-off
