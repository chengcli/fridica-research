"""#69 part b: the driver serves a wave thread (ref fm-wave-e-job1).

The driver keeps the wave under `wave_key(thread)`. Slack posts in the wave thread go through `wave.observe`;
each pass makes one `read_pr` per open job and never polls a merged one; actions go through the existing
`execute_round` path with posts in the wave thread; the wave is saved after every event. The close is posted
once with `ref: fm-wave-<name>-close` and kept in the wave until the daemon takes it.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fridica_research import contracts, wave
from fridica_research.client import ControlError
from fridica_research.driver import Driver
from fridica_research.machine import Event
from fridica_research.store import Store
from support import CFG, OWNER
from test_issue69_wave import H, HEAD, JOBS, PEOPLE, REPO, T0, THREAD, audit_done, fold, handoff, job, opened, polled, post

WS, CH, ROOT = THREAD.split(":")
CLOSE_REF = "fm-wave-d-close"


class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t


class FakeClient:
    """The daemon: a feed, the thread's messages, and posts that echo back onto the feed as the owner's."""

    def __init__(self):
        self.feed: list[dict] = []
        self.messages: dict[str, list[dict]] = {}
        self.posts: list[tuple[str, str]] = []  # (thread, text) of every post the daemon took
        self.refuse: list[str] = []  # refs of posts to refuse once each
        self.ts = 0

    def push(self, sender: str, text: str, turn_kind: str = "") -> str:
        self.ts += 1
        ts = f"1791809900.{self.ts:06d}"
        self.feed.append({"cursor": len(self.feed) + 1, "kind": "message", "workspace": WS, "channel": {"id": CH}, "thread": ROOT, "ts": ts, "sender": sender, "text": text, "turn_kind": turn_kind})
        self.messages.setdefault(THREAD, []).append({"ts": ts, "sender": sender, "text": text})
        return ts

    def events(self, after, limit=1000) -> dict:
        after = after or 0
        page = [e for e in self.feed if e["cursor"] > after][:limit]
        return {"events": page, "next": page[-1]["cursor"] if page else after, "scanned": len(page)}

    def thread(self, thread: str) -> contracts.ThreadView: return contracts.ThreadView(messages=list(self.messages.get(thread, [])))

    def post_message(self, thread: str, req: contracts.PostRequest) -> dict:
        ref = contracts.ref_of(req.text)
        if ref in self.refuse:
            self.refuse.remove(ref)
            raise ControlError(429, "rate_limited")
        self.posts.append((thread, req.text))
        assert thread == THREAD
        self.push(OWNER, req.text, "report")
        return {"ok": True}


class FakeGh:
    """`gh pr list`, `gh pr view` and the reviews call, per PR; records every call."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.prs: dict[int, dict] = {}  # number -> {author, issue, state, head, merge}
        self.reviews: dict[int, list[dict]] = {}

    def run(self, argv, stdin):
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            me = argv[argv.index("--author") + 1]
            return json.dumps([{"number": n, "headRefOid": p["head"], "state": "OPEN", "isDraft": False, "author": {"login": me}, "body": f"Refs #{p['issue']}"}
                               for n, p in sorted(self.prs.items()) if p["author"] == me and p["state"] == "OPEN"])
        if argv[:3] == ["gh", "pr", "view"]:
            p = self.prs[int(argv[3])]
            return json.dumps({"headRefOid": p["head"], "state": p["state"], "isDraft": False, "mergeCommit": {"oid": p.get("merge", "")} if p["state"] == "MERGED" else None})
        if argv[:2] == ["gh", "api"] and "/reviews" in argv[-1]:
            return json.dumps(self.reviews.get(int(argv[-1].split("/pulls/")[1].split("/")[0]), []))
        raise AssertionError(f"unexpected gh call {argv}")

    def reads(self) -> list[str]: return [" ".join(a[:3]) for a in self.calls if a[:3] in (["gh", "pr", "list"], ["gh", "pr", "view"])]

    def merge(self, n: int, sha: str): self.prs[n].update(state="MERGED", merge=sha)


def approve(gh: FakeGh, n: int, login: str, rid: int):
    gh.reviews[n] = [{"id": rid, "user": {"login": login, "type": "User"}, "state": "APPROVED", "commit_id": HEAD, "submitted_at": "2026-10-12T13:00:00Z", "body": ""}]


def make(store: Store, client: FakeClient, gh: FakeGh, clock: Clock) -> Driver:
    return Driver(CFG, client, store, llm=lambda *a: {}, clock=clock, sleep=lambda s: None, github=SimpleNamespace(run=gh.run, poll=lambda *a: None))


def setup(tmp_path, jobs=JOBS):
    store = Store(tmp_path / "s.sqlite3")
    store.set_meta(wave.wave_key(THREAD), opened(jobs).to_json())
    client, gh, clock = FakeClient(), FakeGh(), Clock(T0 + H)
    gh.prs = {71: {"author": "imp1", "issue": 69, "state": "OPEN", "head": HEAD}, 72: {"author": "imp2", "issue": 54, "state": "OPEN", "head": HEAD},
              73: {"author": "imp3", "issue": 61, "state": "OPEN", "head": HEAD}}
    return store, client, gh, clock, make(store, client, gh, clock)


def stored(store: Store) -> wave.Wave: return wave.Wave.from_json(store.get_meta(wave.wave_key(THREAD)))


def pass_reads(drv: Driver, gh: FakeGh) -> list[str]:
    before = len(gh.reads())
    drv.run_once()
    return gh.reads()[before:]


def first_half(store, client, gh, clock, drv):
    """PRs adopted, hand-offs routed by ref, PR link and role, approvals read."""
    assert pass_reads(drv, gh) == ["gh pr list"] * 3  # one read_pr per open job: the list until a PR is recorded
    assert [j.rnd().pr.get("number") for j in stored(store).jobs] == [71, 72, 73]
    clock.t = T0 + 2 * H
    client.push(PEOPLE["imp2"], handoff("aud2", "https://example.org/x", ref="fm-wave-d-job2"))
    client.push(PEOPLE["imp1"], handoff("aud1", f"https://github.com/{REPO}/pull/71"))
    client.push(PEOPLE["imp3"], handoff("aud3", "https://example.org/y"))
    assert pass_reads(drv, gh) == ["gh pr view"] * 3  # then the view (plus its reviews call)
    assert [j.rnd().stage for j in stored(store).jobs] == ["CodeAudit"] * 3
    for n in (1, 2, 3): approve(gh, 70 + n, f"aud{n}", 900 + n)
    clock.t = T0 + 3 * H
    drv.run_once()


def second_half(store, client, gh, clock, drv):
    """Two auditors hand off, the PRs merge on GitHub out of order, the wave closes."""
    clock.t = T0 + 4 * H
    client.push(PEOPLE["aud3"], audit_done("https://example.org/r3"))
    client.push(PEOPLE["aud1"], audit_done("https://example.org/r1"))
    gh.merge(73, "3" * 40)
    assert pass_reads(drv, gh) == ["gh pr view"] * 3
    assert [j.merged for j in stored(store).jobs] == [False, False, True]
    gh.merge(71, "1" * 40)
    clock.t = T0 + 5 * H
    assert pass_reads(drv, gh) == ["gh pr view"] * 2  # a merged job is never polled
    gh.merge(72, "2" * 40)
    clock.t = T0 + 6 * H
    assert pass_reads(drv, gh) == ["gh pr view"]


def closes(client: FakeClient) -> list[str]: return [t for _, t in client.posts if contracts.ref_of(t) == CLOSE_REF]


# -- the driver serves a three-job wave ----------------------------------------------------------
def test_three_job_wave_from_start_to_close_posts_the_close_once(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path)
    first_half(store, client, gh, clock, drv)
    assert [j.rnd().stage for j in stored(store).jobs] == ["Merge"] * 3  # #87: the first approval on the head moves CodeAudit to Merge
    second_half(store, client, gh, clock, drv)
    w = stored(store)
    assert all(j.merged for j in w.jobs) and w.closed_at == T0 + 6 * H and [j.merge_sha for j in w.jobs] == ["1" * 40, "2" * 40, "3" * 40]
    assert len(closes(client)) == 1 and "wave d" in closes(client)[0]
    assert all(t == THREAD for t, _ in client.posts)  # every post lands in the wave thread
    assert not any(":job" in t for t in client.messages)
    for _ in range(3): assert pass_reads(drv, gh) == []
    assert len(closes(client)) == 1 and stored(store).pending == []
    assert store.rounds() == [] and store.all() == []  # a wave is never a round and never a study


def test_restart_mid_wave_resumes_from_the_store(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path)
    first_half(store, client, gh, clock, drv)
    mid = store.get_meta(wave.wave_key(THREAD))
    cursor = store.cursor
    store.close()
    store = Store(tmp_path / "s.sqlite3")  # a new process on the same file
    assert store.get_meta(wave.wave_key(THREAD)) == mid and store.cursor == cursor
    drv = make(store, client, gh, clock)
    second_half(store, client, gh, clock, drv)
    assert len(closes(client)) == 1 and all(j.merged for j in stored(store).jobs)


def test_the_wave_is_saved_after_every_event(tmp_path, monkeypatch):
    store, client, gh, clock, drv = setup(tmp_path)
    saves: list[str] = []
    real = store.set_meta
    monkeypatch.setattr(store, "set_meta", lambda k, v: (saves.append(k) if k == wave.wave_key(THREAD) else None, real(k, v))[1])
    client.push(PEOPLE["imp1"], "progress")
    client.push("USTRANGER", "drive-by")
    drv.run_once()  # two posts and three PR reads: five events
    assert saves.count(wave.wave_key(THREAD)) >= 5
    assert stored(store).seen[-2:] == [client.feed[0]["ts"], client.feed[1]["ts"]]


def test_a_refused_close_is_kept_in_the_wave_and_resent(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path)
    first_half(store, client, gh, clock, drv)
    client.refuse.append(CLOSE_REF)
    second_half(store, client, gh, clock, drv)
    w = stored(store)
    assert w.closed_at is not None and closes(client) == [] and [p["id"] for p in w.pending] == [CLOSE_REF]
    drv.run_once()
    assert len(closes(client)) == 1 and stored(store).pending == []
    drv.run_once()
    assert len(closes(client)) == 1


def test_a_close_that_went_out_before_the_save_is_not_posted_twice(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path)
    first_half(store, client, gh, clock, drv)
    second_half(store, client, gh, clock, drv)
    w = stored(store)
    w.pending = [{"kind": "post", "id": CLOSE_REF, "data": {"thread": THREAD, "post_kind": "report", "text": f"close again\nref: {CLOSE_REF}"}}]
    store.set_meta(wave.wave_key(THREAD), w.to_json())  # a crash between the send and the save
    drv.run_once()
    assert len(closes(client)) == 1 and stored(store).pending == []


# -- the nits, one test each -------------------------------------------------------------------
def test_nit_paths_are_normalized_with_the_trailing_slash_kept_and_a_bare_dir_is_that_dir():
    w = opened([job(1, ["./src//fridica_research/x/../wave.py", "tests//"], "imp1", "aud1"), job(2, ["docs"], "imp2", "aud2")])
    assert w.jobs[0].files == ["src/fridica_research/wave.py", "tests/"] and w.jobs[1].files == ["docs"]
    assert wave.covers("tests", "tests/test_x.py") and wave.covers("tests/test_x.py", "tests") and wave.covers("./a.py", "a.py")
    assert not wave.covers("tests", "tests2/x.py") and not wave.covers("src/a.py", "src/ab.py")
    with pytest.raises(wave.WaveError, match="overlap"):
        opened([job(1, ["tests"], "imp1", "aud1"), job(2, ["tests/test_b.py"], "imp2", "aud2")])
    with pytest.raises(wave.WaveError, match="overlap"):
        opened([job(1, ["src/./a.py"], "imp1", "aud1"), job(2, ["src/b/../a.py"], "imp2", "aud2")])


def test_nit_a_suffixed_ref_routes_to_its_job():
    w = opened()
    j, why = wave.route(w, post("a", "USTRANGER", "PR is up.\nref: fm-wave-d-job1-pr", T0))
    assert j is not None and w.number(j) == 1 and why == ""
    assert w.number(wave.route(w, post("b", "USTRANGER", "ref: fm-wave-d-job3-audit-2", T0))[0]) == 3
    assert wave.route(w, post("c", "USTRANGER", "ref: fm-wave-d-job12", T0))[0] is None  # job 12, not job 1


def test_nit_events_for_a_merged_job_do_not_move_its_round():
    w, _ = fold(opened(), [polled(1, T0 + H, prs=[{"number": 71, "headRefOid": HEAD, "state": "OPEN", "isDraft": False, "author": {"login": "imp1"}, "body": "Refs #69"}]),
                           Event("merged", T0 + 2 * H, {"job": 1, "pr": 71, "sha": "1" * 40})])
    before = w.jobs[0].to_dict()
    assert w.jobs[0].merged and w.jobs[0].rnd().stage == "Implement"
    w, actions = fold(w, [post("h1", PEOPLE["imp1"], handoff("aud1", f"https://github.com/{REPO}/pull/71"), T0 + 3 * H),
                          polled(1, T0 + 3 * H, view={"headRefOid": "9" * 40, "state": "OPEN", "isDraft": False}, reviews=[])])
    assert actions == [] and w.jobs[0].to_dict() == before


def test_nit_a_merged_event_with_another_pr_number_is_a_finding_and_ignored():
    w, _ = fold(opened(), [polled(1, T0 + H, prs=[{"number": 71, "headRefOid": HEAD, "state": "OPEN", "isDraft": False, "author": {"login": "imp1"}, "body": "Refs #69"}])])
    before = w.jobs[0].to_dict()
    w, actions = wave.observe(w, Event("merged", T0 + 2 * H, {"job": 1, "pr": 99, "sha": "9" * 40}), CFG)
    assert actions == [] and w.jobs[0].to_dict() == before and not w.jobs[0].merged
    assert len(w.findings) == 1 and "#99" in w.findings[0] and "#71" in w.findings[0]


def test_nit_an_empty_wave_repo_matches_no_link():
    w = wave.open_wave(THREAD, name="d", repo="", jobs=JOBS, now=T0, cfg=CFG)
    assert wave.route(w, post("a", "USTRANGER", "https://github.com/any/repo/issues/54", T0))[0] is None
    assert wave.route(w, post("b", "USTRANGER", f"https://github.com/{REPO}/issues/69", T0))[0] is None


def test_nit_refuses_two_jobs_on_the_same_issue():
    with pytest.raises(wave.WaveError, match="issue #69"):
        opened([job(69, ["a.py"], "imp1", "aud1"), job(69, ["b.py"], "imp2", "aud2")])


def test_nit_refuses_an_empty_path():
    with pytest.raises(wave.WaveError, match="empty path"):
        opened([job(1, [""], "imp1", "aud1")])
    with pytest.raises(wave.WaveError, match="empty path"):
        opened([job(1, ["a.py", "  "], "imp1", "aud1")])
