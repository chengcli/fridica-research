"""GitHub reviews as the sign-off (R21), PR hygiene (R23), the driver-side merge and late reviews (R24): opt-in.

Enabled by `[github] enabled = true`; with it absent the driver behaves as before (Slack `SIGN-OFF`
lines only, no merge). Every gh call goes through one `Runner` (board.py's), so the
tests answer them with a fake. For the PR of a study in Audit, Deliver or Delivered, `poll`:

- requests the PR's assigned auditor (the study's one peer audit reviewer, #27) once per head through REST
  (`POST repos/<o>/<r>/pulls/<n>/requested_reviewers`), never the PR author and never a bot; a 422 is a finding, never retried;
- reads the PR's fields with `gh pr view --json headRefOid,...` and its reviews from REST
  (`gh api --paginate repos/<o>/<r>/pulls/<n>/reviews`, whose `commit_id` binds a review to a head; gh's
  `latestReviews` leaves the oid empty); a review counts only when it is by the PR's assigned auditor
  (not a bot, not the PR's author; others are findings) and on the current head: APPROVED -> `sign_off approve`, CHANGES_REQUESTED ->
  `sign_off changes` (login -> Slack id through `Config.slack_of`), COMMENTED -> a finding without
  verdict; each counted review is mirrored into the study thread as one SIGN-OFF line that the
  driver never parses back (`contracts.MIRROR_MARK`), and once more as DISMISSED (with a finding) if GitHub dismisses it;
  a delivered approval that stops counting is withdrawn once (`sign_off dismissed`), reopening the scope it approved;
- for a `[repos]` entry with `merge = "driver"` squash-merges an open, non-draft PR once the assigned auditor's
  verdict on the current head is APPROVED (`--match-head-commit`; GitHub's branch protection enforces the rest), and
  records the squash sha; for `merge = "owner"` it posts the approved PR once and waits;
- after the merge, each CHANGES_REQUESTED review (every one, keyed by its id) gets one reply on the PR and one line in the thread
  ("acknowledged, goes into the next PR"), its items become findings, and the next PR body to that
  repository lists them under "From post-merge review by <login>".

Every thread post and machine event is an `Item`, marked seen only after the caller delivered it.
State lives in the store's `meta` table (`github:pr:<thread>:<owner/repo#N>`, `github:carry:<repo>`, `github:open:<repo>:<head>`),
never in the machine's snapshot: the machine learns about reviews only through events.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field

from .roles import roles_table
from . import contracts
from .board import M_ADD_ITEM, Projects, Runner, subprocess_runner
from .config import Config
from .machine import ORDER, Event, State

log = logging.getLogger("fridica_research.github")
PR_FIELDS = "headRefOid,state,mergedAt,mergeCommit,author,milestone,isDraft"  # the PR's own fields; reviews come from REST (`reviews`)
POLLED_STAGES = ("Audit", "Deliver", "Delivered")
VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")  # a COMMENTED review never supersedes a login's verdict
Q_PR = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){id}}}"
ACK = "acknowledged, goes into the next PR"


class PrHygieneError(ValueError):
    """A PR body without `Closes #N`, the study thread, the board link or the milestone (R23)."""


@dataclass
class Item:
    """One delivery to the study: thread posts or machine events; marked seen (`key`) only after it was delivered."""
    key: str
    posts: list[tuple[str, str]] = field(default_factory=list)  # (ref suffix, text) for the study thread
    events: list[Event] = field(default_factory=list)


@dataclass
class Poll:
    items: list[Item] = field(default_factory=list)
    close: bool = False  # the PR needs no more polls once every item was delivered
    retry: bool = False  # an effect failed this poll (an acknowledgement): the PR stays polled so it is tried again

    @property
    def posts(self) -> list[tuple[str, str]]: return [p for i in self.items for p in i.posts]
    @property
    def events(self) -> list[Event]: return [e for i in self.items for e in i.events]


def iso(ts: float) -> str: return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def parse_iso(s: str) -> float: return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


def review_items(body: str) -> list[str]:
    """The items of a review body: its non-empty lines without list markers, each cut at 500 characters ("(no text)" for an empty body)."""
    items = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line).strip()[:500] for line in (body or "").splitlines()]
    return [i for i in items if i] or ["(no text)"]


def json_values(text: str) -> list:
    """The JSON values of `gh api --paginate` output, which prints one array per page back to back."""
    out, dec, i, text = [], json.JSONDecoder(), 0, text or ""
    while True:
        while i < len(text) and text[i].isspace(): i += 1
        if i >= len(text): return out
        v, i = dec.raw_decode(text, i)
        out.append(v)


def latest_verdicts(reviews: list[dict]) -> dict[str, dict]:
    """login (lower case) -> that login's latest APPROVED / CHANGES_REQUESTED / DISMISSED review: greatest submitted_at, then id."""
    out: dict[str, dict] = {}
    for r in sorted((r for r in reviews if r["state"] in VERDICTS), key=lambda r: (r["at"], r["n"])): out[r["login"].lower()] = r
    return out


class GitHub:
    def __init__(self, cfg: Config, runner: Runner | None = None, remember=lambda k, v: None, recall=lambda k: None):
        self.cfg, self.g = cfg, cfg.github
        self.run = runner or subprocess_runner(cfg.github.token_env)
        self.remember, self.recall = remember, recall
        self.projects = Projects(cfg, self.run, remember, recall)

    # -- gh primitives --------------------------------------------------------------
    def api(self, method: str, path: str, body: dict | None = None) -> str:
        return self.run(["gh", "api", "-X", method, path, *(["--input", "-"] if body is not None else [])], json.dumps(body) if body is not None else None)

    def view(self, pr: str) -> dict:
        repo, n = contracts.pr_id(pr)
        return json.loads(self.run(["gh", "pr", "view", n, "-R", repo, "--json", PR_FIELDS], None))

    def reviews(self, pr: str) -> list[dict]:
        """Every submitted review of the PR from REST, which carries `commit_id` (`gh pr view --json latestReviews` leaves the commit oid and id empty)."""
        repo, n = contracts.pr_id(pr)
        pages = json_values(self.run(["gh", "api", "--paginate", f"repos/{repo}/pulls/{n}/reviews?per_page=100"], None))
        out = []
        for r in (r for page in pages for r in (page if isinstance(page, list) else [page])):
            if not r.get("submitted_at"): continue  # a pending review
            u = r.get("user") or {}
            out.append({"id": str(r.get("id", "")), "n": int(r.get("id") or 0), "login": str(u.get("login", "")), "type": str(u.get("type", "")), "state": r.get("state"), "commit": r.get("commit_id") or "", "at": r["submitted_at"], "body": r.get("body") or ""})
        return sorted(out, key=lambda r: (r["at"], r["n"]))

    def is_bot(self, author: dict | None) -> bool:
        a = author or {}
        login = str(a.get("login", "")).lower()
        return bool(a.get("is_bot")) or a.get("type") == "Bot" or login.endswith("[bot]") or login in {b.lower() for b in self.g.bots}

    def request_reviews(self, pr: str, logins: list[str]):
        repo, n = contracts.pr_id(pr)
        self.api("POST", f"repos/{repo}/pulls/{n}/requested_reviewers", {"reviewers": logins})

    def request_each(self, pr: str, logins: list[str], done: list[str], refused: list[str]):
        """L4: one request per login; a login GitHub refuses (HTTP 422: no access) goes to `refused` and is never retried."""
        for lg in logins:
            if lg in done or lg in refused: continue
            try: self.request_reviews(pr, [lg])
            except Exception as e:  # noqa: BLE001 - other failures are retried on the next poll
                if "HTTP 422" not in str(e):
                    log.warning("review request for %s on %s failed: %s", lg, pr, e)
                    continue
                refused.append(lg)
                continue
            done.append(lg)

    def merge(self, pr: str, head: str):
        """`gh pr merge --squash` pinned to the head that was approved."""
        repo, n = contracts.pr_id(pr)
        self.run(["gh", "pr", "merge", n, "-R", repo, "--squash", "--match-head-commit", head], None)

    def comment(self, pr: str, body: str):
        repo, n = contracts.pr_id(pr)
        self.api("POST", f"repos/{repo}/issues/{n}/comments", {"body": body})

    def milestone(self, repo: str, title: str) -> int:
        """The number of the generation milestone `title` (R1, R2, ...), created on demand."""
        for m in json.loads(self.run(["gh", "api", f"repos/{repo}/milestones?state=all&per_page=100"], None) or "[]"):
            if m.get("title") == title: return int(m["number"])
        return int(json.loads(self.api("POST", f"repos/{repo}/milestones", {"title": title}))["number"])

    def set_milestone(self, pr: str, title: str):
        repo, n = contracts.pr_id(pr)
        self.run(["gh", "api", "-X", "PATCH", f"repos/{repo}/issues/{n}", "-F", f"milestone={self.milestone(repo, title)}"], None)

    def add_to_project(self, pr: str) -> str:
        repo, n = contracts.pr_id(pr)
        owner, name = repo.split("/", 1)
        node = self.projects.graphql(Q_PR, owner=owner, name=name, number=int(n))["repository"]["pullRequest"]["id"]
        return self.projects.graphql(M_ADD_ITEM, project=self.projects.project().id, content=node)["addProjectV2ItemById"]["item"]["id"]

    # -- R23: opening a PR ------------------------------------------------------------
    def board_url(self) -> str:
        b = self.cfg.board
        if not (b.enabled and b.owner and b.number): return ""
        return f"https://github.com/{'orgs' if b.owner_type == 'organization' else 'users'}/{b.owner}/projects/{b.number}"

    def carried(self, repo: str) -> dict:
        return json.loads(self.recall(f"github:carry:{repo.lower()}") or "{}")

    def next_pr_body(self, repo: str, summary: str, closes: int | None, thread: str, generation: int) -> str:
        """The R23 body for the next PR to `repo`, with the post-merge items carried from earlier reviews (R24)."""
        owner = self.cfg.login_of(self.cfg.owner) or self.cfg.owner or "unassigned"
        names = {"explorer": owner, "debater": owner, "implementer": owner, "auditor": ", ".join([r.login or self.cfg.login_of(r.handle) for r in self.cfg.reviewers]) or "unassigned", "driver and arbitrator": owner}
        return contracts.pr_body(summary + "\n\n" + roles_table(names), closes, thread, self.board_url(), f"R{generation}", self.carried(repo))

    def existing_pr(self, repo: str, head: str) -> str:
        prs = json.loads(self.run(["gh", "pr", "list", "-R", repo, "--head", head, "--state", "open", "--json", "url"], None) or "[]")
        return prs[0]["url"] if prs else ""

    def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> str:
        """Refuses a body without the R23 lines; otherwise creates the PR, adds it to the project, sets the milestone, requests the reviewers.

        Resumable: each step is recorded (store meta `github:open:<repo>:<head>`) once done, and the PR of an earlier
        attempt is found again (`gh pr list --head`), so a retry after a failed step completes the rest without a second PR."""
        missing = contracts.pr_hygiene_missing(body)
        if missing: raise PrHygieneError("refusing to open a PR without " + ", ".join(missing))
        if "| Role | Holder | Responsibility |" not in body:
            owner = self.cfg.login_of(self.cfg.owner) or self.cfg.owner or "unassigned"
            body += "\n\n" + roles_table({**{r: owner for r in ("explorer", "debater", "implementer", "driver and arbitrator")}, "auditor": ", ".join([r.login or self.cfg.login_of(r.handle) for r in self.cfg.reviewers]) or "unassigned"})
        key = f"github:open:{repo.lower()}:{head}"
        p = json.loads(self.recall(key) or "{}")
        save = lambda: self.remember(key, json.dumps(p, sort_keys=True))  # noqa: E731
        if not p.get("url"):
            p["url"] = self.existing_pr(repo, head)
            if not p["url"]:
                p["url"], p["body"] = self.run(["gh", "pr", "create", "-R", repo, "--head", head, "--base", base, "--title", title, "--body", body], None).strip().splitlines()[-1], body
            save()
        url = p["url"]
        if p.get("body") and not p.get("uncarried"):  # only items written into this PR's body leave the carry, once
            self.uncarry(repo, p["body"])
            p["uncarried"] = True
            save()
        if not p.get("project"):
            self.add_to_project(url)
            p["project"] = True
            save()
        if not p.get("milestone"):
            self.set_milestone(url, contracts.pr_milestone(body))
            p["milestone"] = True
            save()
        logins = [lg for lg in self.reviewers(url) if not self.is_bot({"login": lg})]
        done, refused = p.setdefault("requested", []), p.setdefault("refused", [])
        self.request_each(url, logins, done, refused)
        save()
        for lg in refused: log.warning("review request for %s on %s refused (no access); stays on the Slack fallback", lg, url)
        if len(done) + len(refused) < len(logins): raise RuntimeError(f"review requests on {url} incomplete; run again to finish")
        return url

    def uncarry(self, repo: str, body: str):
        """Removes from the carry each item `body` lists under its login's post-merge heading; an item added since stays for the next PR."""
        shown: dict[str, list[str]] = {}
        login = None
        for line in body.splitlines():
            if line.startswith("## "): login = line[3 + len(contracts.POST_MERGE_HEAD):] if line[3:].startswith(contracts.POST_MERGE_HEAD) else None
            elif login is not None and line.startswith("- "): shown.setdefault(login, []).append(line[2:])
        left = {}
        for lg, items in self.carried(repo).items():
            rest, written = [], shown.get(lg, [])
            for i in items:
                if i in written: written.remove(i)
                else: rest.append(i)
            if rest: left[lg] = rest
        self.remember(f"github:carry:{repo.lower()}", json.dumps(left, sort_keys=True) if left else None)

    # -- R21, R24: the poll ------------------------------------------------------------
    def poll(self, state: State, now: float, deliver=None) -> Poll:
        """One poll of the study's PR. Each `Item` is handed to `deliver` (posts, then machine events) and marked seen
        only once that returned; a delivery that raises stops the poll and is produced again on the next one."""
        out = Poll()
        pr = state.implementer.get("pr", "")
        repo, n = contracts.pr_id(pr)
        if not (self.g.enabled and repo): return out
        key = f"github:pr:{state.thread}:{repo}#{n}"
        t = json.loads(self.recall(key) or "{}")
        if not (state.stage in POLLED_STAGES or (t.get("merged_at") and state.stage in ORDER)): return out  # a merged PR stays polled when a late review reopened its study
        if t.get("done") or now < t.get("last_poll", 0) + self.g.poll_interval: return out
        t["last_poll"] = now
        seen = t.setdefault("seen", [])
        try:
            v = self.view(pr)
            rs = self.reviews(pr)
            self.hygiene(pr, state, v, t)
            self.request(pr, state, v, t, out, now)
            self.review_events(pr, state, v, rs, t, out, now)
            self.withdrawals(pr, state, v, rs, t, out, now)
            self.gate(pr, state, v, rs, t, out, now)
            for item in out.items:
                if deliver is not None: deliver(item)
                seen.append(item.key)
            if out.close and not out.retry: t["done"] = True  # only after every item was delivered and no effect waits for a retry
        finally:
            self.remember(key, json.dumps(t, sort_keys=True))
        return out

    def hygiene(self, pr: str, state: State, v: dict, t: dict):
        """R23 for the PR under audit: on the study's project and on the generation milestone (each step once, retried until it succeeds)."""
        try:
            if self.board_url() and not t.get("project"):
                self.add_to_project(pr)
                t["project"] = True
            if not v.get("milestone") and not t.get("milestone"): self.set_milestone(pr, f"R{state.generation}")
            t["milestone"] = True
        except Exception as e:  # noqa: BLE001 - hygiene never stalls the sign-off
            log.warning("PR hygiene for %s failed: %s", pr, e)

    def reviewers(self, pr: str) -> list[str]:
        """The logins configured as reviewers of the PR's repository: `[repos]` `reviewers`, else the `[audit]` reviewers' logins."""
        repo, _ = contracts.pr_id(pr)
        logins = self.cfg.repo(repo).reviewers or tuple(r.login for r in self.cfg.reviewers if r.login)
        return list(dict.fromkeys(logins))

    def auditor(self, state: State) -> str:
        """The PR's assigned auditor (#27: one reviewer per PR): the GitHub login of the study's peer audit reviewer, "" unless exactly one."""
        logins = {self.cfg.login_of(sc["reviewer"], state.people) for sc in state.audit_scopes.values() if sc.get("reviewer")}
        return next(iter(logins)) if len({lg.lower() for lg in logins}) == 1 else ""

    def request(self, pr: str, state: State, v: dict, t: dict, out: Poll, now: float):
        """R24: the assigned auditor is requested, once per head; never the author (an implementer never reviews their PR) or a bot."""
        head = v.get("headRefOid", "")
        repo, n = contracts.pr_id(pr)
        auditor = self.auditor(state)
        if not auditor and "no-auditor" not in t["seen"]: out.items.append(Item("no-auditor", events=[Event("finding", now, {"text": f"{repo}#{n} has no single assigned auditor with a GitHub login in the study's audit scopes: no review is requested and the driver does not merge it"})]))
        if v.get("state") == "OPEN":
            if t.get("requested_head") != head: t["requested_head"], t["requested"] = head, []
            author = str((v.get("author") or {}).get("login", "")).lower()
            logins = [lg for lg in [auditor] if lg and lg.lower() != author and not self.is_bot({"login": lg})]
            self.request_each(pr, logins, t["requested"], t.setdefault("refused", []))
        for lg in t.get("refused", []):
            if f"refused:{lg}" not in t["seen"]: out.items.append(Item(f"refused:{lg}", events=[Event("finding", now, {"text": f"GitHub refused the review request for {lg} on {repo}#{n} (no access); {lg} stays on the Slack SIGN-OFF fallback"})]))

    def counts(self, pr: str, state: State, v: dict, login: str) -> str:
        """"" when `login`'s review counts (the PR's assigned auditor, not the PR's author), else why not."""
        repo, n = contracts.pr_id(pr)
        if login.lower() == str((v.get("author") or {}).get("login", "")).lower(): return "the PR's author"
        if login.lower() != self.auditor(state).lower(): return f"not the assigned auditor of {repo}#{n}"
        return ""

    def review_events(self, pr: str, state: State, v: dict, rs: list[dict], t: dict, out: Poll, now: float):
        head = v.get("headRefOid", "")
        merged_at = v.get("mergedAt") or t.get("merged_at")
        seen = t["seen"]
        repo, n = contracts.pr_id(pr)
        latest = latest_verdicts(rs)
        for r in rs:
            rid, kind, login = r["id"], r["state"], r["login"]
            if kind == "DISMISSED" and f"post:review-{rid}" in seen and f"dismissed:{rid}" not in seen:
                # F3: a counted review GitHub now shows as dismissed (by hand, or as stale after a push) is taken back in the thread and the findings
                out.items.append(Item(f"dismissed:{rid}", posts=[(f"dismissed-{rid}", contracts.mirror_line(login, kind, pr, r["commit"]))], events=[Event("finding", now, {"text": f"GitHub review by {login} on {repo}#{n} at {r['commit'][:12]} was dismissed: it no longer counts toward the merge"})]))
            if rid in seen: continue
            if kind not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED") or self.is_bot(r) or r["commit"] != head:
                seen.append(rid)  # bots never count; a review of an older head never becomes current
                continue
            why = self.counts(pr, state, v, login)
            if why:
                out.items.append(Item(rid, events=[Event("finding", now, {"text": f"GitHub review by {login} on {repo}#{n} at {head[:12]} ({kind}) not counted: {why}: {r['body'].strip()[:500] or '(no text)'}"})]))
                continue
            after = bool(merged_at and r["at"] > merged_at)
            if f"post:review-{rid}" not in seen: out.items.append(Item(f"post:review-{rid}", posts=[(f"review-{rid}", contracts.mirror_line(login, kind, pr, head, after))]))
            if kind != "COMMENTED" and not after and latest.get(login.lower()) is not r:
                out.items.append(Item(rid))  # a superseded verdict before the merge is mirrored but is not the login's; every post-merge review is its own
                continue
            events = []
            if kind == "COMMENTED":
                events.append(Event("finding", now, {"text": f"GitHub review by {login} on {repo}#{n} at {head[:12]} (COMMENTED, no verdict): {r['body'].strip()[:500] or '(no text)'}"}))
            elif slack := self.cfg.slack_of(login, state.people):
                events.append(Event("sign_off", now, {"sender": slack, "pr": pr, "sha": head, "verdict": "approve" if kind == "APPROVED" else "changes"}))
                if kind == "APPROVED": t.setdefault("approvals", {})[rid] = slack  # withdrawn from that sender once it stops counting (`withdrawals`)
            else:
                events.append(Event("finding", now, {"text": f"GitHub review by {login} on {repo}#{n} at {head[:12]} ({kind}): no Slack id maps to {login}, so no audit card closes on it; map it in [people]"}))
            if kind == "CHANGES_REQUESTED" and after:
                acked = self.acknowledge(pr, login, r, t, out, now)
                if acked is None:  # B1: the reply failed; the review stays unseen and is acknowledged on a later poll
                    out.retry = True
                    continue
                events += acked
            out.items.append(Item(rid, events=events))

    def acknowledge(self, pr: str, login: str, r: dict, t: dict, out: Poll, now: float) -> list[Event] | None:
        """R24: a changes-requested review after the merge: one reply on the PR, one thread line, findings, carried into the next PR.

        None when the reply failed or waits for its backoff: nothing else is produced, and a later poll tries the reply again
        (each effect once). The reply is tried `ack_tries` times, `ack_backoff` doubling between tries; then it is a finding
        and the rest of the acknowledgement goes ahead without it. State is kept per (PR, reviewer, review id) in `acks`."""
        repo, n = contracts.pr_id(pr)
        rid = r["id"]
        items = review_items(r["body"])
        if rid not in t.setdefault("carried", []):  # before the reply, so a retried reply keeps its items in the order the reviews were submitted
            carry = self.carried(repo)
            carry[login] = [*carry.get(login, []), *items]
            self.remember(f"github:carry:{repo.lower()}", json.dumps(carry, sort_keys=True))
            t["carried"].append(rid)
        a = t.setdefault("acks", {}).setdefault(f"{login.lower()}:{rid}", {"tries": 0, "next": 0})
        if rid not in t.setdefault("acked", []) and a["tries"] < self.g.ack_tries:
            if now < a["next"]: return None
            try:
                self.comment(pr, f"@{login} {ACK}.")
                t["acked"].append(rid)
            except Exception as e:  # noqa: BLE001 - retried after the backoff, up to ack_tries
                a["tries"] += 1
                log.warning("acknowledgement on %s failed (%d of %d): %s", pr, a["tries"], self.g.ack_tries, e)
                if a["tries"] < self.g.ack_tries:
                    a["next"] = now + self.g.ack_backoff * 2 ** (a["tries"] - 1)
                    return None
        if rid not in t["acked"] and f"ack-failed:{rid}" not in t["seen"]: out.items.append(Item(f"ack-failed:{rid}", events=[Event("finding", now, {"text": f"the reply to the post-merge review by {login} on {repo}#{n} failed {a['tries']} times and is not retried; reply on the PR by hand"})]))
        if f"post:ack-{rid}" not in t["seen"]: out.items.append(Item(f"post:ack-{rid}", posts=[(f"ack-{rid}", f"{ACK}: post-merge review by {login} on {repo}#{n}")]))
        return [Event("finding", now, {"text": f"post-merge review by {login} on {repo}#{n}: {item}"}) for item in items]

    def withdrawals(self, pr: str, state: State, v: dict, rs: list[dict], t: dict, out: Poll, now: float):
        """An approval that was delivered as `sign_off approve` and no longer counts (dismissed, superseded by a later verdict of
        its login that is not an approval, on a head that is no longer the PR's, or its login no longer the assigned auditor)
        is withdrawn once: `sign_off dismissed` reopens the scope it approved as a later `changes` does, and a finding says why."""
        head, seen = v.get("headRefOid", ""), t["seen"]
        repo, n = contracts.pr_id(pr)
        latest, by_id = latest_verdicts(rs), {r["id"]: r for r in rs}
        for rid, slack in t.get("approvals", {}).items():
            r = by_id.get(rid)
            if r is None or rid not in seen or f"withdrawn:{rid}" in seen: continue
            last = latest.get(r["login"].lower(), r)
            superseded = last is not r and last["state"] != "APPROVED" and not (last["state"] == "CHANGES_REQUESTED" and last["commit"] == head)  # a changes on the head reopens by itself
            why = ("dismissed" if r["state"] == "DISMISSED" else f"the PR's head is now {head[:12]}" if r["commit"] != head
                   else f"superseded by a later {last['state']} review" if superseded else self.counts(pr, state, v, r["login"]))
            if not why: continue
            events = [Event("sign_off", now, {"sender": slack, "pr": pr, "sha": r["commit"], "verdict": "dismissed"})]
            if why != "dismissed": events.append(Event("finding", now, {"text": f"GitHub approval by {r['login']} on {repo}#{n} at {r['commit'][:12]} no longer counts ({why}): it is withdrawn from the audit"}))  # a dismissal has its own finding (F3)
            out.items.append(Item(f"withdrawn:{rid}", events=events))

    def gate(self, pr: str, state: State, v: dict, rs: list[dict], t: dict, out: Poll, now: float):
        """R23/R24 merge: `merge = "driver"` repos only, never a draft, once the assigned auditor's latest verdict on the current head
        is APPROVED (a DISMISSED one is not); head binding, staleness and CI are GitHub's branch protection."""
        repo, n = contracts.pr_id(pr)
        if v.get("state") == "MERGED":
            t.setdefault("merged_at", v.get("mergedAt"))
            if not t.get("merged_sha"): t["merged_sha"] = (v.get("mergeCommit") or {}).get("oid")
            if t.get("merge"): self.merged(state, t, out)
            if t.get("merged_at") and now > parse_iso(t["merged_at"]) + self.g.post_merge_window: out.close = True
            return
        if v.get("state") != "OPEN":
            out.close = True
            return
        head = v.get("headRefOid", "")
        on_head = [r for r in latest_verdicts(rs).values() if r["commit"] == head and not self.is_bot(r) and not self.counts(pr, state, v, r["login"])]
        approvers = sorted(r["login"] for r in on_head if r["state"] == "APPROVED")
        if not approvers or any(r["state"] == "CHANGES_REQUESTED" for r in on_head): return
        if self.cfg.repo(repo).merge != "driver":
            if f"post:approved-{head[:12]}" not in t["seen"]: out.items.append(Item(f"post:approved-{head[:12]}", posts=[(f"approved-{head[:12]}", f"{pr} approved on {head[:12]} by {', '.join(approvers)}; {repo} is merged by its owner")]))
            return
        if v.get("isDraft"):  # GitHub refuses to merge a draft: no attempt, one finding per head
            if f"draft:{head[:12]}" not in t["seen"]: out.items.append(Item(f"draft:{head[:12]}", events=[Event("finding", now, {"text": f"{repo}#{n} is approved on {head[:12]} but is a draft: the driver merges it once it is marked ready for review"})]))
            return
        after = None
        try: self.merge(pr, head)
        except Exception as e:  # noqa: BLE001 - retried on the next poll, unless GitHub merged it anyway
            log.warning("merge of %s failed: %s", pr, e)
            try: after = self.view(pr)
            except Exception as e2:  # noqa: BLE001 - retried on the next poll
                log.warning("view of %s after its failed merge failed: %s", pr, e2)
                return
            if after.get("state") != "MERGED" or after.get("headRefOid") != head: return
        t["merge"] = {"head": head, "approvers": approvers}  # L2: the revision and the merged line follow from a view, now or on the next poll
        if after is None:
            try: after = self.view(pr)
            except Exception as e:  # noqa: BLE001 - the merge stands; the next poll's view records it
                log.warning("view of %s after its merge failed: %s", pr, e)
                return
        if after.get("state") == "MERGED": self.gate(pr, state, {**after, "mergedAt": after.get("mergedAt") or iso(now)}, rs, t, out, now)

    def merged(self, state: State, t: dict, out: Poll):
        """After the driver's merge: the squash sha is the generation's revision, and one `merged` line goes to the thread."""
        m, sha = t["merge"], t.get("merged_sha")
        if not sha: return
        pr = state.implementer.get("pr", "")
        repo, n = contracts.pr_id(pr)
        self.remember(f"github:revision:{repo.lower()}:g{state.generation}", sha)  # the squash sha is the generation's revision
        k = f"merged-{m['head'][:12]}"
        if f"post:{k}" not in t["seen"]: out.items.append(Item(f"post:{k}", posts=[(k, f"merged {repo}#{n} (squash) as {sha} after approval by {', '.join(m['approvers'])} on {m['head'][:12]}")]))
