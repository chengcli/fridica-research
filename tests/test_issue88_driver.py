"""#88, #91, #93: the driver asks the auditor for the GitHub review, finds a merge nobody watched, and stops polling
what is over (ref fm-wave-g-assign).

Each new head of a job's open PR gets the job's auditor requested through `github.request_reviewers`, once per
(job, head) in memory; any failure but a 422 is logged and requested again on the next poll, a 422 gives one durable
notify. `list_prs_for_issue` runs only when adoption found nothing, and a MERGED PR there is the job's `merged`
event (sha empty when the listing has none). A dropped job and a closed wave are not polled.
"""
from __future__ import annotations

import json

from fridica_research import contracts, wave
from test_issue69_wave import H, HEAD, THREAD
from test_issue69b_driver import FakeGh, make, pass_reads, setup, stored

NEW = "d" * 40


class ReviewGh(FakeGh):
    """FakeGh that also takes review requests and answers the issue listing (`gh pr list --state all`)."""

    def __init__(self):
        super().__init__()
        self.requests: list[tuple[int, list[str]]] = []
        self.request_errors: list[str] = []  # raised by the next review requests, one each
        self.issue_prs: list[dict] = []

    def run(self, argv, stdin):
        if argv[:2] == ["gh", "api"] and argv[-3].endswith("/requested_reviewers"):
            self.calls.append(argv)
            if self.request_errors: raise RuntimeError(self.request_errors.pop(0))
            self.requests.append((int(argv[-3].split("/pulls/")[1].split("/")[0]), json.loads(stdin)["reviewers"]))
            return "{}"
        if argv[:3] == ["gh", "pr", "list"] and "--state" in argv and argv[argv.index("--state") + 1] == "all":
            self.calls.append(argv)
            return json.dumps(self.issue_prs)
        return super().run(argv, stdin)

    def issue_lookups(self) -> int: return sum(1 for a in self.calls if a[:3] == ["gh", "pr", "list"] and "all" in a)


def world(tmp_path, jobs=None):
    store, client, _, clock, _ = setup(tmp_path, **({"jobs": jobs} if jobs else {}))
    gh = ReviewGh()
    gh.prs = {71: {"author": "imp1", "issue": 69, "state": "OPEN", "head": HEAD}, 72: {"author": "imp2", "issue": 54, "state": "OPEN", "head": HEAD},
              73: {"author": "imp3", "issue": 61, "state": "OPEN", "head": HEAD}}
    return store, client, gh, clock, make(store, client, gh, clock)


def refused(client) -> list[str]: return [t for _, t in client.posts if "review-refused" in (contracts.ref_of(t) or "")]


# -- #88: the auditor's review is requested on GitHub ------------------------------------------------
def test_auditor_review_requested_once_per_job_and_head(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    drv.run_once()  # adoption records each PR and its head
    assert sorted(gh.requests) == [(71, ["aud1"]), (72, ["aud2"]), (73, ["aud3"])]
    for _ in range(3):
        clock.t += H
        drv.run_once()
    assert len(gh.requests) == 3  # the same head is never requested twice
    gh.prs[72]["head"] = NEW
    clock.t += H
    drv.run_once()
    assert gh.requests[3:] == [(72, ["aud2"])]  # a new head: requested again, that job only


def test_a_failed_request_is_logged_and_retried_on_the_next_poll(tmp_path, caplog):
    store, client, gh, clock, drv = world(tmp_path)
    gh.request_errors = ["HTTP 502: Bad Gateway"]
    drv.run_once()
    assert len(gh.requests) == 2 and "review request for wave" in caplog.text
    clock.t += H
    drv.run_once()
    assert sorted(gh.requests) == [(71, ["aud1"]), (72, ["aud2"]), (73, ["aud3"])] and refused(client) == []


def test_a_422_gives_one_durable_notify_and_no_retry(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    gh.request_errors = ["HTTP 422: Reviews may only be requested from collaborators"]
    client.refuse = [f"fm-wave-d-job1-review-refused-{HEAD[:12]}"] * 5  # the daemon refuses the sends of this pass: the notify stays pending
    drv.run_once()
    assert [p["id"] for p in stored(store).pending] == [f"fm-wave-d-job1-review-refused-{HEAD[:12]}"] and refused(client) == []
    client.refuse = []
    for _ in range(3):
        clock.t += H
        drv.run_once()
    posts = refused(client)
    assert len(posts) == 1 and "aud1" in posts[0] and "PR #71" in posts[0] and "HTTP 422" in posts[0]
    assert stored(store).pending == [] and sorted(n for n, _ in gh.requests) == [72, 73]  # job 1's head is not requested again


# -- #91: a PR that merged while nobody was watching ---------------------------------------------------
def test_issue_listing_only_when_adoption_found_nothing(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    gh.prs.pop(73)
    drv.run_once()
    assert gh.issue_lookups() == 1  # job 3 only: jobs 1 and 2 adopted their PR
    assert [j.rnd().pr.get("number") for j in stored(store).jobs] == [71, 72, None]


def test_a_merged_pr_found_for_the_issue_is_the_jobs_merged_event(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    gh.prs.pop(73)
    gh.issue_prs = [{"number": 74, "title": "board", "body": "Closes #61", "state": "MERGED", "isDraft": False, "author": {"login": "bot"},
                     "headRefOid": NEW, "mergedAt": "2026-10-12T15:00:00Z"},
                    {"number": 75, "title": "other", "body": "Refs #61", "state": "OPEN", "isDraft": False, "author": {"login": "x"}, "headRefOid": NEW, "mergedAt": None}]
    drv.run_once()
    j3 = stored(store).jobs[2]
    assert j3.merged and j3.merge_sha == "" and j3.rnd().pr["number"] == 74 and j3.rnd().pr["state"] == "MERGED"
    clock.t += H
    assert pass_reads(drv, gh) == ["gh pr view"] * 2  # merged: never read again


# -- #93: dropped jobs and closed waves are not polled ------------------------------------------------
def test_a_dropped_job_is_not_polled(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    w, _ = wave.drop_job(stored(store), 2, "descoped", now=clock.t)
    store.set_meta(wave.wave_key(THREAD), w.to_json())
    assert pass_reads(drv, gh) == ["gh pr list"] * 2
    assert not any("--author" in a and "imp2" in a for a in gh.calls) and not any(n == 72 for n, _ in gh.requests)


def test_a_closed_wave_is_not_polled(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    w = stored(store)
    w.closed_at = clock.t
    store.set_meta(wave.wave_key(THREAD), w.to_json())
    for _ in range(2):
        drv.run_once()
        clock.t += H
    assert gh.calls == []


def issue_pr(number: int, body: str, merged_at: str | None, title: str = "x") -> dict:
    return {"number": number, "title": title, "body": body, "state": "MERGED", "isDraft": False, "author": {"login": "other"}, "headRefOid": NEW, "mergedAt": merged_at}


def test_a_pr_merged_before_the_wave_opened_is_ignored(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    gh.prs.pop(73)
    gh.issue_prs = [issue_pr(40, "Refs #61", "2020-01-01T00:00:00Z"), issue_pr(41, "Closes #61", None)]  # merged in 2020; mergedAt missing
    drv.run_once()
    j3 = stored(store).jobs[2]
    assert not j3.merged and j3.rnd().pr.get("number") is None and gh.issue_lookups() == 1


def test_a_pr_that_only_mentions_the_issue_is_ignored(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    gh.prs.pop(73)
    gh.issue_prs = [issue_pr(76, "Follow-up to #61, see there.", "2026-10-12T15:00:00Z", title="see #61")]
    drv.run_once()
    assert not stored(store).jobs[2].merged


def test_the_lowest_qualifying_pr_number_wins(tmp_path):
    store, client, gh, clock, drv = world(tmp_path)
    gh.prs.pop(73)
    gh.issue_prs = [issue_pr(79, "Fixes #61", "2026-10-12T16:00:00Z"), issue_pr(77, "Refs #61", "2026-10-12T15:00:00Z"),
                    issue_pr(60, "Refs #61", "2020-01-01T00:00:00Z"), issue_pr(70, "see #61", "2026-10-12T15:00:00Z")]
    drv.run_once()
    j3 = stored(store).jobs[2]
    assert j3.merged and j3.rnd().pr["number"] == 77  # 60 merged before the wave, 70 has no Closes/Refs line
