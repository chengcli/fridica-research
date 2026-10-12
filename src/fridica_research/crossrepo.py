"""R22 cross-repo work as a pure module (#66): file an issue in another repo, and track its PR.

Nothing here runs `gh` or reads a clock. `file_issue` returns the `gh issue create` argv for the caller to
run, and `track(prev, view)` folds one `gh pr view --json` read into the next tracked state and the actions
the caller executes. The action kinds are exactly `card_update`, `notify_implementer` and `finding`.

- The repo must be listed in `cfg.repos` (R23 `[repos]`); anything else is refused.
- The issue body carries the study thread, the board link and the contract or finding, as the
  `Study thread:` / `Board:` lines of `contracts.pr_body`.
- The first poll (`prev` None or empty) is a baseline: a card update only, no notification.
- A change of state, of the review set, of `reviewDecision` or of `headRefOid` notifies the PR's
  implementer (its author) only, plus a card update; a new CHANGES_REQUESTED also records a finding.
- R22 Amendment 1: nothing here requests or chases a review.
- Reviews are compared as `(author.login, state)` pairs of `latestReviews`: `gh pr view` leaves the
  review id and `commit.oid` empty. `reviewDecision` is "" when the repo requires no review.
"""
from __future__ import annotations

from .config import Config
from .machine import Action

ACTION_KINDS = ("card_update", "notify_implementer", "finding")
VIEW_FIELDS = "number,url,author,state,reviewDecision,latestReviews,headRefOid"  # the `gh pr view --json` fields `track` reads


def board_url(cfg: Config) -> str:
    """The project board link, as `GitHub.board_url` builds it; "" when the board is off."""
    b = cfg.board
    if not (b.enabled and b.owner and b.number): return ""
    return f"https://github.com/{'orgs' if b.owner_type == 'organization' else 'users'}/{b.owner}/projects/{b.number}"


def issue_body(body: str, thread: str, board: str, contract: str) -> str:
    return "\n".join([body.strip(), "", f"Study thread: {thread}", f"Board: {board or '(no board)'}", "", "Contract or finding:", contract.strip()])


def file_issue(repo: str, title: str, body: str, cfg: Config, *, thread: str, contract: str) -> list[str]:
    """The `gh issue create` argv for an issue in `repo`, which must be in `cfg.repos`."""
    known = {r.name.lower() for r in cfg.repos}
    if str(repo).lower() not in known:
        raise ValueError(f"{repo!r} is not in [repos] ({', '.join(sorted(known)) or 'none listed'}); the driver files issues only in listed repositories")
    if not str(thread).strip(): raise ValueError("an issue needs its study thread")
    if not str(contract).strip(): raise ValueError("an issue needs the contract or finding it carries")
    return ["gh", "issue", "create", "-R", repo, "--title", title, "--body", issue_body(body, thread, board_url(cfg), contract)]


def review_pairs(view: dict) -> list[list[str]]:
    """`(author.login, state)` per latest review, sorted; id and commit oid are empty in `gh pr view`, so they are never used."""
    return sorted([str((r.get("author") or {}).get("login", "")), str(r.get("state", ""))] for r in view.get("latestReviews") or [])


def snapshot(view: dict) -> dict:
    return {"number": view.get("number"), "url": view.get("url", ""), "implementer": str((view.get("author") or {}).get("login", "")),
            "state": str(view.get("state", "")), "reviews": review_pairs(view), "decision": str(view.get("reviewDecision") or ""), "head": str(view.get("headRefOid", ""))}


def track(prev: dict | None, view: dict) -> tuple[dict, list[Action]]:
    """One poll of the PR: the next state and the actions. Pure.

    `prev` None or empty is the baseline. Every action id carries the transition number `n` (0 at the
    baseline, +1 per change), so a comment then an approval at one head, or CR -> dismissed -> CR, each
    get their own ids; a finding's id also names the review's login and `submittedAt`."""
    s = snapshot(view)
    key = f"{s['url'] or s['number']}"
    n = int(prev.get("n", 0)) + 1 if prev else 0
    s["n"] = n
    card = Action("card_update", f"crossrepo/{key}/{n}/card", {"pr": s["url"], "state": s["state"], "decision": s["decision"], "head": s["head"], "reviews": s["reviews"]})
    if not prev: return s, [card]  # baseline: no notification, no finding
    changed = [k for k in ("state", "reviews", "decision", "head") if prev.get(k) != s[k]]
    if not changed:
        s["n"] = prev.get("n", 0)
        return s, []
    actions = [card]
    new = [p for p in s["reviews"] if p not in (prev.get("reviews") or [])]
    submitted = {(str((r.get("author") or {}).get("login", "")), str(r.get("state", ""))): str(r.get("submittedAt", "")) for r in view.get("latestReviews") or []}
    for login, state in new:
        if state == "CHANGES_REQUESTED":
            actions.append(Action("finding", f"crossrepo/{key}/{n}/changes/{login}/{submitted.get((login, state), '')}", {"pr": s["url"], "text": f"{login} requested changes on {s['url'] or key} at {s['head'][:12]}"}))
    if s["implementer"]:
        what = ", ".join(changed)
        actions.append(Action("notify_implementer", f"crossrepo/{key}/{n}/notify", {"to": s["implementer"], "pr": s["url"], "changed": changed,
                              "text": f"@{s['implementer']} {s['url'] or key} changed ({what}): state {s['state']}, decision {s['decision'] or '(none required)'}, head {s['head'][:12]}"}))
    return s, actions
