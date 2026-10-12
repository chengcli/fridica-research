"""#69 part a: the wave and job model (ref fm-wave-d-job1).

A `Wave` holds N `Job`s, each wrapping an `arbiter.Round` on the short track with thread id `<thread>:jobN`.
`open_wave` refuses a missing ETA (R49), overlapping files, two Config jobs, an empty file list, and one
person in both roles. Thread posts route by the `ref: fm-wave-<w>-jobN` line, then a PR or issue link,
then the author's role in exactly one job; a `merged` event is ground truth; the last merge emits `wave_close`.
"""
from __future__ import annotations

import json

import pytest

from fridica_research import arbiter, wave
from fridica_research.machine import Event
from support import CFG, OWNER

THREAD = "T1:C1:1791769427.483009"
REPO = "chengcli/fridica-research"
T0 = 1_700_000_000.0
H = 3600.0
HEAD = "c" * 40
PEOPLE = {"imp1": "U0IMP1", "aud1": "U0AUD1", "imp2": "U0IMP2", "aud2": "U0AUD2", "imp3": "U0IMP3", "aud3": "U0AUD3"}


def person(login: str) -> dict: return {"slack": PEOPLE[login], "login": login}


def job(issue: int, files: list[str], imp: str, aud: str, **kw) -> dict:
    return {"issue": issue, "files": files, "implementer": person(imp), "auditor": person(aud), "input_at": T0, "pr_eta": T0 + 2 * H, "review_eta": T0 + 4 * H, **kw}


JOBS = [job(69, ["src/fridica_research/wave.py", "src/fridica_research/cli.py", "tests/"], "imp1", "aud1"),
        job(54, ["src/fridica_research/register.py"], "imp2", "aud2", config_change=True),
        job(61, ["src/fridica_research/board.py", "docs/"], "imp3", "aud3")]


def opened(jobs=JOBS, **kw) -> wave.Wave: return wave.open_wave(THREAD, name="d", repo=REPO, jobs=jobs, now=T0, cfg=CFG, **kw)


def pr(number: int, login: str, issue: int) -> dict:
    return {"number": number, "headRefOid": HEAD, "state": "OPEN", "isDraft": False, "author": {"login": login}, "body": f"Refs #{issue}"}


def review(rid: int, login: str, state: str = "APPROVED") -> dict:
    return {"id": str(rid), "n": rid, "login": login, "type": "User", "state": state, "commit": HEAD, "at": "2026-10-12T03:00:00Z", "body": ""}


def post(ts: str, sender: str, text: str, now: float) -> Event: return Event("crew_post", now, {"ts": ts, "sender": sender, "text": text, "post_kind": "", "files": 0})
def polled(n: int, now: float, **data) -> Event: return Event("pr_polled", now, {"job": n, **data})


def handoff(aud: str, link: str, ref: str = "") -> str:
    return f"PR is open, handing off to <@{PEOPLE[aud]}>.\n{link}\nDue 10-12 09:00 ET" + (f"\nref: {ref}" if ref else "")


def audit_done(link: str) -> str: return f"Approved on GitHub, over to the driver.\n{link}\nDue 10-12 12:00 ET"


def fold(w: wave.Wave, events: list[Event]) -> tuple[wave.Wave, list[dict]]:
    out: list[dict] = []
    for ev in events:
        w, actions = wave.observe(w, ev, CFG)
        out += [a.to_dict() for a in actions]
    return w, out


def three_job_tape() -> list[Event]:
    """Three jobs run in parallel: PRs adopted, hand-offs routed three different ways, approvals, merges out of order."""
    t = T0
    tape = [polled(1, t + H, prs=[pr(71, "imp1", 69)]), polled(2, t + H, prs=[pr(72, "imp2", 54)]), polled(3, t + 1.5 * H, prs=[pr(73, "imp3", 61)])]
    tape += [post("1.1", PEOPLE["imp2"], handoff("aud2", "https://example.org/x", ref="fm-wave-d-job2"), t + 2 * H),  # by ref
             post("1.2", PEOPLE["imp1"], handoff("aud1", f"https://github.com/{REPO}/pull/71"), t + 2.1 * H),  # by PR link
             post("1.3", PEOPLE["imp3"], handoff("aud3", "https://example.org/y"), t + 2.2 * H),  # by role
             post("1.4", "USTRANGER", "Drive-by comment with no job.", t + 2.3 * H),  # a finding
             post("1.5", OWNER, "Floor manager prose, never routed.", t + 2.4 * H)]
    view = {"headRefOid": HEAD, "state": "OPEN", "isDraft": False}
    tape += [polled(n, t + 3 * H + n * 60, view=view, reviews=[review(900 + n, f"aud{n}")]) for n in (1, 2, 3)]
    tape += [post("2.1", PEOPLE["aud3"], audit_done("https://example.org/r3"), t + 4 * H), post("2.2", PEOPLE["aud1"], audit_done("https://example.org/r1"), t + 4.1 * H)]
    tape += [Event("merged", t + 5 * H, {"job": 3, "pr": 73, "sha": "3" * 40}), Event("merged", t + 5.5 * H, {"pr": 71, "sha": "1" * 40}),
             Event("time", t + 6 * H, {}), Event("merged", t + 7 * H, {"job": 2, "pr": 72, "sha": "2" * 40})]
    return tape


# -- opening and the refusals -------------------------------------------------------------
def test_open_wave_holds_three_jobs_each_wrapping_a_round_keyed_per_job():
    w = opened()
    assert [j.issue for j in w.jobs] == [69, 54, 61] and w.name == "d" and w.channel == "C1"
    for n, j in enumerate(w.jobs, 1):
        r = j.rnd()
        assert r.thread == f"{THREAD}:job{n}" and r.stage == "Implement" and r.order == list(wave.JOB_ORDER) and r.issue == j.issue and r.repo == REPO
        assert r.holders("implementer") == [j.implementer["slack"]] and r.login("auditor") == j.auditor["login"]
        assert r.due == T0 + arbiter.budget(CFG, "Implement")
        assert (j.pr_eta, j.review_eta, j.input_at) == (T0 + 2 * H, T0 + 4 * H, T0)  # recorded as given, never computed
    assert wave.Wave.from_json(w.to_json()).to_json() == w.to_json()


@pytest.mark.parametrize("missing", ["pr_eta", "review_eta"])
def test_refuses_a_job_missing_either_eta(missing):
    with pytest.raises(wave.WaveError, match=rf"R49.*|{missing}"):
        opened([{**JOBS[0], missing: None}])


def test_refuses_overlapping_files_equal_or_nested_only():
    nested = [job(1, ["src/a.py", "tests/"], "imp1", "aud1"), job(2, ["tests/test_b.py"], "imp2", "aud2")]
    with pytest.raises(wave.WaveError, match="overlap.*'tests/'"): opened(nested)
    equal = [job(1, ["src/a.py"], "imp1", "aud1"), job(2, ["src/a.py"], "imp2", "aud2")]
    with pytest.raises(wave.WaveError, match="overlap"): opened(equal)
    assert wave.covers("src", "src/") and wave.covers("src/", "src/x/y.py") and wave.covers("src/x/y.py", "src/")
    assert not wave.covers("src/a.py", "src/ab.py") and not wave.covers("tests/", "tests2/x.py") and not wave.covers("src/*.py", "src/a.py")  # no glob guessing
    assert opened([job(1, ["src/a.py", "src/b/"], "imp1", "aud1"), job(2, ["src/ab.py", "src/bb/"], "imp2", "aud2")]).jobs


def test_refuses_two_config_change_jobs():
    with pytest.raises(wave.WaveError, match="Config"):
        opened([job(1, ["a.py"], "imp1", "aud1", config_change=True), job(2, ["b.py"], "imp2", "aud2", config_change=True)])


def test_refuses_an_empty_file_list():
    with pytest.raises(wave.WaveError, match="file list is empty"):
        opened([job(1, [], "imp1", "aud1")])


def test_refuses_one_person_as_both_implementer_and_auditor():
    with pytest.raises(wave.WaveError, match="both implementer and auditor"):
        opened([job(1, ["a.py"], "imp1", "imp1")])
    with pytest.raises(wave.WaveError, match="both implementer and auditor"):  # the same login under another Slack id
        opened([{**job(1, ["a.py"], "imp1", "aud1"), "auditor": {"slack": "U0OTHER", "login": "IMP1"}}])


# -- the fold --------------------------------------------------------------------------
def test_three_job_fold_is_deterministic_on_replay():
    w0 = opened()
    w1, a1 = fold(w0, three_job_tape())
    w2, a2 = fold(w0, three_job_tape())
    assert w1.to_json() == w2.to_json() and a1 == a2
    assert all(j.merged for j in w1.jobs) and w1.closed_at == T0 + 7 * H
    assert all(a["thread"] == THREAD for a in a1)  # every action is rewritten to the wave thread
    assert {a["id"].split("/")[0] for a in a1 if a["kind"] != "wave_close"} == {f"{THREAD}:job{n}" for n in (1, 2, 3)}  # refs keep the job suffix
    assert [a["kind"] for a in a1].count("wave_close") == 1 and a1[-1]["kind"] == "wave_close"
    assert [j.rnd().pr["number"] for j in w1.jobs] == [71, 72, 73]
    assert [j.delivered for j in w1.jobs] == [{"pr": T0 + H, "review": T0 + 3 * H + 60}, {"pr": T0 + H, "review": T0 + 3 * H + 120}, {"pr": T0 + 1.5 * H, "review": T0 + 3 * H + 180}]
    assert [j.rnd().stage for j in w1.jobs] == ["Merge"] * 3  # #87: job 2's auditor never handed off, and its approval on the head moved it to Merge
    assert [f for f in w1.findings if "not routed" in f] == ["crew_post 1.4 not routed: USTRANGER holds no role and the post names no job"]
    assert not any("merge-ready" in f for f in w1.findings)


def test_persist_and_resume_mid_wave_gives_the_same_fold():
    tape = three_job_tape()
    straight, actions = fold(opened(), tape)
    half, first = fold(opened(), tape[:9])
    resumed = wave.Wave.from_json(half.to_json())  # what the store holds between two driver passes
    assert resumed.to_json() == half.to_json() and resumed.jobs[0].rnd().stage == "Merge"
    back, rest = fold(resumed, tape[9:])
    assert back.to_json() == straight.to_json() and first + rest == actions


def test_wave_close_carries_first_eta_delivered_pairs_and_the_eta_score():
    w, actions = fold(opened(), three_job_tape())
    c = actions[-1]
    assert c["kind"] == "wave_close" and c["id"] == f"{THREAD}/wave/close" and c["thread"] == THREAD and c["wave"] == "d"
    assert [(e["job"], e["kind"], e["first_eta"], e["delivered"]) for e in c["etas"]] == [
        (1, "pr", T0 + 2 * H, T0 + H), (1, "review", T0 + 4 * H, T0 + 3 * H + 60), (2, "pr", T0 + 2 * H, T0 + H), (2, "review", T0 + 4 * H, T0 + 3 * H + 120),
        (3, "pr", T0 + 2 * H, T0 + 1.5 * H), (3, "review", T0 + 4 * H, T0 + 3 * H + 180)]
    assert c["score"]["jobs_scored"] == 6 and c["score"]["late"] == 0 and c["score"]["early"] == 6 and c["score"]["bias_hours"] == pytest.approx(-(1 + 1 + 0.5 + (59 / 60) + (58 / 60) + (57 / 60)) / 6)
    assert [(m["job"], m["pr"], m["sha"]) for m in c["jobs"]] == [(1, 71, "1" * 40), (2, 72, "2" * 40), (3, 73, "3" * 40)]
    assert json.loads(json.dumps(c)) == c


def test_routing_order_ref_then_link_then_role_else_a_finding():
    w = opened()
    w, _ = fold(w, [polled(1, T0, prs=[pr(71, "imp1", 69)])])
    # the ref line wins over a link and the author's role
    j, why = wave.route(w, post("a", PEOPLE["imp1"], f"https://github.com/{REPO}/pull/71\nref: fm-wave-d-job3", T0))
    assert w.number(j) == 3 and why == ""
    assert wave.route(w, post("b", PEOPLE["imp1"], "ref: fm-wave-c-job1", T0))[0] is None  # another wave's ref
    assert wave.route(w, post("c", PEOPLE["imp1"], "ref: fm-wave-d-job9", T0))[0] is None
    # a link: the recorded PR or the job's issue, on the wave's repo only
    assert w.number(wave.route(w, post("d", "USTRANGER", f"https://github.com/{REPO}/issues/54", T0))[0]) == 2
    assert w.number(wave.route(w, post("e", "USTRANGER", f"https://github.com/{REPO}/pull/71", T0))[0]) == 1
    assert wave.route(w, post("f", "USTRANGER", "https://github.com/other/repo/issues/54", T0))[0] is None
    assert "links name jobs 1, 2" in wave.route(w, post("g", "USTRANGER", f"https://github.com/{REPO}/pull/71 https://github.com/{REPO}/issues/54", T0))[1]
    # the author's role, in exactly one job
    assert w.number(wave.route(w, post("h", PEOPLE["aud3"], "progress", T0))[0]) == 3
    both = opened([job(1, ["a.py"], "imp1", "aud1"), job(2, ["b.py"], "imp2", "aud1")])
    assert "holds a role in jobs 1, 2" in wave.route(both, post("i", PEOPLE["aud1"], "progress", T0))[1]
    w, actions = wave.observe(w, post("j", PEOPLE["aud1"], "progress", T0), CFG)
    assert actions == [] and w.findings == [] and w.seen == ["j"]
    w, _ = wave.observe(w, post("j", "USTRANGER", "the same ts again", T0), CFG)
    assert w.findings == []  # a ts already seen is never a second finding


def test_a_merged_event_is_ground_truth_even_when_the_job_was_not_ready():
    w = opened([JOBS[0]])
    w, actions = wave.observe(w, Event("merged", T0 + H, {"pr": 71, "sha": "1" * 40}), CFG)
    assert actions == [] and not w.jobs[0].merged and "not routed" in w.findings[0]  # no job has PR 71 recorded and no job number was given
    w, actions = wave.observe(w, Event("merged", T0 + H, {"job": 1, "pr": 71, "sha": "1" * 40}), CFG)
    j = w.jobs[0]
    assert j.merged and j.merged_at == T0 + H and j.merge_sha == "1" * 40 and j.rnd().pr == {"state": "MERGED", "number": 71}
    assert j.delivered == {"pr": T0 + H, "review": T0 + H} and any("before it was merge-ready (round at Implement)" in f for f in w.findings)
    assert [a.kind for a in actions] == ["wave_close"] and w.closed_at == T0 + H
    again, actions = wave.observe(w, Event("merged", T0 + 2 * H, {"job": 1, "pr": 71, "sha": "1" * 40}), CFG)
    assert actions == [] and again.to_json() == w.to_json()


def test_a_jobs_round_behaves_as_the_arbiters_round_and_posts_go_to_the_wave_thread():
    w = opened()
    w, actions = fold(w, [post("1", PEOPLE["imp1"], handoff("aud1", "https://example.org/x"), T0)])
    assert actions == [] and "Implement" in w.jobs[0].rnd().held  # held until a PR is recorded, as in slice 2
    w, actions = fold(w, [polled(1, T0 + H, prs=[pr(71, "imp1", 69)])])
    assert [a["kind"] for a in actions] == ["board_update"] and actions[0]["thread"] == THREAD and actions[0]["id"].startswith(f"{THREAD}:job1/round/Implement-CodeAudit/board/")
    assert w.jobs[0].rnd().stage == "CodeAudit" and w.jobs[1].rnd().stage == "Implement"
    w, actions = fold(w, [post("2", PEOPLE["imp2"], "Handing off.\nhttps://example.org/y\nDue 10-12 09:00 ET", T0 + H), polled(2, T0 + 2 * H, prs=[pr(72, "imp2", 54)])])
    fix = [a for a in actions if a["kind"] == "post"]
    assert len(fix) == 1 and fix[0]["thread"] == THREAD and f"ref: {THREAD}:job2/round/Implement/mention/{PEOPLE['imp2']}" in fix[0]["text"] and f"<@{PEOPLE['aud2']}>" in fix[0]["text"]
    assert all(p["thread"] == THREAD for p in w.jobs[1].rnd().pending)
    assert all(j.rnd().misses == {} for j in w.jobs if j.issue != 54) and w.jobs[1].rnd().misses == {f"Implement/{PEOPLE['imp2']}": 1}


# -- parse_job and the CLI -------------------------------------------------------------
def test_parse_job_and_parse_time():
    j = wave.parse_job("issue=69;files=src/a.py, tests/;implementer=U0IMP1:imp1;auditor=U0AUD1:aud1;input_at=2026-10-12T01:30Z;pr_eta=2026-10-12T02:20:00+00:00;review_eta=1791777600;config_change=yes")
    assert j == {"issue": 69, "files": ["src/a.py", "tests/"], "implementer": {"slack": "U0IMP1", "login": "imp1"}, "auditor": {"slack": "U0AUD1", "login": "aud1"},
                 "input_at": 1791768600.0, "pr_eta": 1791771600.0, "review_eta": 1791777600.0, "config_change": True}
    with pytest.raises(wave.WaveError): wave.parse_job("issue=1;round=x")
    with pytest.raises(wave.WaveError): wave.parse_job("bogus")
    assert wave.parse_time(None) is None and wave.parse_time("") is None and wave.parse_time("2026-10-12T02:20") == 1791771600.0


def test_cli_wave_open_and_status(tmp_path, capsys):
    from fridica_research import cli
    from fridica_research.store import Store
    cfgp = tmp_path / "research.toml"
    cfgp.write_text(f'channels = ["C1"]\nstate_path = "{tmp_path / "s.sqlite3"}"\n[fridica]\nsocket = "{tmp_path / "c.sock"}"\nowner = "{OWNER}"\n')
    j1 = "issue=69;files=src/fridica_research/wave.py,tests/;implementer=U0IMP1:imp1;auditor=U0AUD1:aud1;pr_eta=2026-10-12T02:20Z;review_eta=2026-10-12T04:00Z"
    j2 = "issue=54;files=src/fridica_research/register.py;implementer=U0IMP2:imp2;auditor=U0AUD2:aud2;pr_eta=2026-10-12T14:00Z;review_eta=2026-10-12T16:00Z;config_change=yes"
    assert cli.main(["--config", str(cfgp), "wave", "status", THREAD]) == 1
    assert cli.main(["--config", str(cfgp), "wave", "open", THREAD, "--name", "d", "--repo", REPO, "--job", j1, "--job", j2.replace("review_eta=2026-10-12T16:00Z", "")]) == 1
    assert "R49" in capsys.readouterr().err
    assert cli.main(["--config", str(cfgp), "wave", "open", THREAD, "--name", "d", "--repo", REPO, "--job", j1, "--job", j2]) == 0
    out = capsys.readouterr().out
    assert "opened wave d" in out and "2 job(s)" in out and "job 1\t#69\tImplement\tPR -\tPR ETA 10-11 22:20 ET\treview ETA 10-12 00:00 ET\timp1 -> aud1\t2 paths\t0 findings" in out
    assert "job 2\t#54\tImplement" in out and "1 paths Config" in out
    assert cli.main(["--config", str(cfgp), "wave", "open", THREAD, "--job", j1]) == 1  # already open
    assert "already an open wave" in capsys.readouterr().err
    store = Store(tmp_path / "s.sqlite3")
    w = wave.Wave.from_json(store.get_meta(wave.wave_key(THREAD)))
    assert [j.issue for j in w.jobs] == [69, 54] and store.rounds() == []  # a wave is never a `round:` key
    store.close()
    assert cli.main(["--config", str(cfgp), "wave", "status", THREAD]) == 0
    assert f"wave d\t{THREAD}\topen\t0/2 merged\t0 findings" in capsys.readouterr().out
