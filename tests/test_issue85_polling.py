"""#85: wave PR polling honours `poll_interval`, surfaces GitHub failures, survives a daemon outage (ref fm-wave-f-assign).

`poll_waves` keeps a per-job `last_poll` and reads a job's PR at most once per `cfg.github.poll_interval`;
`poll_rounds` does the same per adopted round. The failure count is kept in memory only.
A pass with any failed read adds 1 to the wave's failure count and a clean pass resets it; at 3 one durable
notify (`ref: fm-wave-<name>-gh-failing`) goes on `Wave.pending`, and no more while the reads keep failing.
`poll_waves` still runs when the daemon is Unavailable. A failed `ref:` probe is "unknown": the send is skipped.
"""
from __future__ import annotations

from fridica_research import arbiter, contracts
from fridica_research.client import ControlError, Unavailable
from fridica_research.machine import Action
from test_issue69_wave import H, THREAD
from test_issue69b_driver import pass_reads, setup, stored
import test_issue46_guards as guards
from test_issue46_pr import ISSUE, later, pr
from test_issue46_pr import setup as round_setup

GH_FAILING = "fm-wave-d-gh-failing"
world = guards.world  # the adopted-round fixture of #46


def test_poll_waves_honours_poll_interval(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path)
    assert pass_reads(drv, gh) == ["gh pr list"] * 3
    clock.t += 1.0  # the next pass, well inside poll_interval (120 s)
    assert pass_reads(drv, gh) == []
    clock.t += drv.cfg.github.poll_interval
    assert pass_reads(drv, gh) == ["gh pr view"] * 3  # due again: the PRs are recorded, so the view


def notifies(store) -> list[dict]: return [p for p in stored(store).pending if p["id"] == GH_FAILING]


def test_three_failed_passes_raise_one_durable_notify(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path)
    client.refuse = [GH_FAILING]  # the first send is refused: the notify stays pending
    drv.github.run = lambda argv, stdin: (_ for _ in ()).throw(RuntimeError("HTTP 401: Bad credentials"))
    for _ in range(2):
        drv.run_once()
        clock.t += H
        assert notifies(store) == []
    drv.run_once()
    pending = notifies(store)
    assert len(pending) == 1 and pending[0]["kind"] == "notify_owner" and "HTTP 401" in pending[0]["text"]
    for _ in range(4):  # keeps failing: no second notify, the pending one is delivered once
        clock.t += H
        drv.run_once()
    sent = [t for _, t in client.posts if contracts.ref_of(t) == GH_FAILING]
    assert len(sent) == 1 and notifies(store) == []


def test_clean_pass_resets_failure_count(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path)
    ok = gh.run
    bad = lambda argv, stdin: (_ for _ in ()).throw(RuntimeError("timeout"))  # noqa: E731
    for run in (bad, bad, ok, bad, bad):
        drv.github.run = run
        drv.run_once()
        clock.t += H
    assert notifies(store) == [] and not [t for _, t in client.posts if contracts.ref_of(t) == GH_FAILING]


def test_poll_waves_runs_while_daemon_unavailable(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path)
    drv.recovered = True
    def down(after, limit=1000): raise Unavailable("connection refused")
    client.events = down
    assert pass_reads(drv, gh) == ["gh pr list"] * 3
    assert [j.rnd().pr.get("number") for j in stored(store).jobs] == [71, 72, 73]


def test_failed_probe_skips_the_send(tmp_path):
    store, client, gh, clock, drv = setup(tmp_path)
    def broken(thread): raise ControlError(503, "probe failed")
    client.thread = broken
    a = Action("post", "fm-wave-d-x", {"thread": THREAD, "post_kind": "report", "text": "hello\nref: fm-wave-d-x"})
    assert drv.execute_round(arbiter.Round(thread=THREAD), a, thread=THREAD) is not None  # kept pending
    assert client.posts == []


def test_poll_rounds_honours_poll_interval(world):
    """An adopted round's PR is read at most once per `poll_interval`, as a wave job's is."""
    server, store, drv, gh = round_setup(world, "Implement")
    gh.prs = [pr(82, "imp", f"Closes #{ISSUE}")]
    drv.run_once()
    drv.clock.t += 1.0
    drv.run_once()
    assert gh.reads() == ["gh pr list"]  # inside poll_interval: no second read
    later(drv)
    drv.run_once()
    assert gh.reads() == ["gh pr list", "gh pr view", "gh api --paginate"]
