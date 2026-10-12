"""#83, #92: the driver runs the R51 ladder for wave jobs (ref fm-wave-h-assign, job 1).

`tick_waves()` runs after `poll_waves` on every pass, and not at all when the daemon is unavailable. Each open job
keeps its ladder state under `ladder:<thread>:job<N>`, keyed on (stage, holder, eta); a new key resets `done` and
`checkin_at`. `last_reply` is the driver clock when the holder posts. A blocking finding is an entry in `Round.held`:
W is counted from the held post, with one deputy wake per held post. `notify_owner` goes out as a plain post.
"""
from __future__ import annotations

import json

from fridica_research import contracts, wave
from fridica_research.client import Unavailable
from fridica_research.machine import Action
from support import OWNER
from test_issue69_wave import H, PEOPLE, T0, THREAD
from test_issue69b_driver import FakeClient, make, stored
from test_issue88_driver import world

MIN = 60.0


class InstructClient(FakeClient):
    """FakeClient that also takes `instruct` (the deputy wake-up) and can be made unavailable."""

    def __init__(self):
        super().__init__()
        self.instructs: list[tuple[str, str, str]] = []  # (thread, client_id, text)
        self.down = False

    def instruct(self, thread, text, client_id, actor="owner"):
        self.instructs.append((thread, client_id, text))
        return {"ok": True}

    def events(self, after, limit=1000):
        if self.down: raise Unavailable("socket gone")
        return super().events(after, limit)


def ladder_world(tmp_path, prs=True):
    store, _, gh, clock, _ = world(tmp_path)
    client = InstructClient()
    if not prs: gh.prs = {}
    return store, client, gh, clock, make(store, client, gh, clock)


def meta(store, n: int) -> dict: return json.loads(store.get_meta(f"ladder:{THREAD}:job{n}") or "{}")
def refs(client) -> list[str]: return [contracts.ref_of(t) for _, t in client.posts]
def wakes(client) -> list[str]: return [t.rsplit("ref: ", 1)[1] for _, _, t in client.instructs]



def rung(n: int, eta: float, name: str) -> str:
    """A ladder rung id since #108: `<ref>/<holder>@<eta>/<rung>`."""
    return f"fm-wave-d-job{n}/{PEOPLE[f'imp{n}']}@{int(eta)}/{name}"

def test_one_checkin_per_job_at_the_eta(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path, prs=False)
    drv.run_once()
    assert not any(r and r.endswith("/checkin") for r in refs(client))  # before the ETA
    clock.t = T0 + 2 * H
    for _ in range(3):
        drv.run_once()
        clock.t += MIN
    assert sorted(r for r in refs(client) if r and r.endswith("/checkin")) == [rung(n, T0 + 2 * H, "checkin") for n in (1, 2, 3)]
    m = meta(store, 1)
    assert m["key"] == ["pr", PEOPLE["imp1"], T0 + 2 * H] and m["checkin_at"] == T0 + 2 * H and rung(1, T0 + 2 * H, "checkin") in m["done"]


def test_silence_past_eta_plus_w_wakes_the_deputy_once_and_a_reply_counts(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path, prs=False)
    clock.t = T0 + 2 * H
    drv.run_once()
    clock.t = T0 + 2 * H + 10 * MIN
    client.push(PEOPLE["imp1"], "still on it, PR in 20 min")
    drv.run_once()
    assert meta(store, 1)["last_reply"] == T0 + 2 * H + 10 * MIN  # the driver clock, not the Slack ts
    clock.t = T0 + 2 * H + 31 * MIN  # W = clamp(0.25 x 2 h) = 30 min
    for _ in range(2):
        drv.run_once()
        clock.t += MIN
    assert sorted(wakes(client)) == [rung(2, T0 + 2 * H, "escalate"), rung(3, T0 + 2 * H, "escalate")]  # job 1 replied after its check-in


def test_tick_runs_after_poll_waves(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path)
    order = []
    poll, tick = drv.poll_waves, drv.tick_waves
    drv.poll_waves = lambda: (order.append("poll"), poll())[1]
    drv.tick_waves = lambda: (order.append("tick"), tick())[1]
    drv.run_once()
    assert order == ["poll", "tick"]


def test_no_tick_when_the_daemon_is_unavailable(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path, prs=False)
    drv.run_once()
    client.down = True
    clock.t = T0 + 2 * H
    drv.run_once()
    assert client.posts == [] and client.instructs == [] and meta(store, 1)["checkin_at"] is None
    client.down = False
    drv.run_once()
    assert rung(1, T0 + 2 * H, "checkin") in refs(client)


def test_a_new_eta_resets_done_and_checkin(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path, prs=False)
    clock.t = T0 + 2 * H
    drv.run_once()
    assert meta(store, 1)["done"] and meta(store, 1)["checkin_at"] is not None
    w = wave.set_eta(stored(store), 1, pr_eta=T0 + 5 * H)
    store.set_meta(wave.wave_key(THREAD), w.to_json())
    clock.t += MIN
    drv.run_once()
    m = meta(store, 1)
    assert m["key"] == ["pr", PEOPLE["imp1"], T0 + 5 * H] and m["done"] == [] and m["checkin_at"] is None


def test_the_review_stage_holds_the_auditor_and_the_review_eta(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path)
    drv.run_once()  # PRs adopted: the PR is delivered, the auditor holds the job
    assert meta(store, 2)["key"] == ["review", PEOPLE["aud2"], T0 + 4 * H]


def held(store, n: int, stage: str, ts: str):
    w = stored(store)
    w.jobs[n - 1].round["held"] = {stage: {"ts": ts, "sender": PEOPLE[f"imp{n}"], "text": "hand-off"}}
    store.set_meta(wave.wave_key(THREAD), w.to_json())


def test_a_held_post_wakes_the_deputy_once_past_w_from_the_post(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path, prs=False)
    drv.run_once()
    ts = f"{T0 + H:.6f}"
    held(store, 1, "Implement", ts)
    clock.t = T0 + H + 29 * MIN
    drv.run_once()
    assert wakes(client) == []
    clock.t = T0 + H + 31 * MIN  # W = 30 min, counted from the held post, not from the ETA
    for _ in range(3):
        drv.run_once()
        clock.t += MIN
    assert wakes(client) == [f"fm-wave-d-job1/held/{ts}"]


def test_each_held_post_gets_its_own_wake(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path, prs=False)
    drv.run_once()
    held(store, 1, "Implement", f"{T0:.6f}")
    clock.t = T0 + H + 1 * MIN
    drv.run_once()
    held(store, 1, "Implement", f"{T0 + H - 40 * MIN:.6f}")
    clock.t += MIN
    drv.run_once()
    assert wakes(client) == [f"fm-wave-d-job1/held/{T0:.6f}", f"fm-wave-d-job1/held/{T0 + H - 40 * MIN:.6f}"]


def test_notify_owner_is_a_plain_post(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path)
    text = f"<@{OWNER}> scope question on fm-wave-d-job1: x\nref: fm-wave-d-job1/question/1"
    drv.execute_ladder(THREAD, Action("notify_owner", "fm-wave-d-job1/question/1", {"thread": THREAD, "post_kind": "report", "text": text}))
    assert [t for _, t in client.posts] == [text] and client.posts[0][1].count(f"<@{OWNER}>") == 1
