"""The driver and arbitrator for an adopted, people-run round (#46, R47): `observe(round, event, cfg) -> (round, actions)`.

A separate component next to `machine.py`, not a mode inside it. `machine.py` stays the AI study
engine and is untouched, so its replay corpora are unaffected. Like `step`, this fold is pure: no
I/O and no clock, every event carries `now`, and the round is the fold of `observe` over the
thread's messages.

What R47 asks of it, and what this first slice does:
- R47.1 people-run is the default. A round is adopted per thread (`adopt`, driver-only); an
  adopted thread never starts a study and this module emits no `delegate`, `set_driver` or
  `llm_call` action at all (`ACTION_KINDS`). `delegate = true` on a person-held role is refused
  at adoption, so no AI path exists.
- R47.2 advance on clearance. A message clears the current stage when it comes from a holder of
  that stage's role and carries a hand-off marker, the next holder's mention, a link and a due
  time. The stage, role and assignee move at once and the due time is re-based from the event's
  `now` with the role's budget from `[crew_budget]` (kept apart from the AI `[projection]`
  defaults). Board writes are field-level only -- never `Board.sync_study`, never an issue-body
  edit (that is #49).
- R47.3/L3.1 no clock-driven closure. A time event produces nothing here: the arbiter arms no
  timers and has no overrun, retry or delivery path. The ETA ladder is a later slice.
- R47.4/G7a the one fix the arbiter makes: a hand-off that carries a link and a due time but not
  the next holder's mention gets exactly one post adding it, counted as a miss. That post is the
  only time the arbiter mentions anyone -- a hand-off's own mention is the ping (Provision 03).
- G9 no post of this module asks a person to run a command.

Idempotence: every post carries a deterministic `ref:` line recorded in `posted_refs`, the fold
ignores a message ts it has already seen, and the driver probes the thread by `ref:` before
posting, the way `Driver.recover` does.

Out of this slice: PR adoption and polling, the ETA ladder, merge and the queue, per-role AI
opt-in execution.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from dataclasses import dataclass, field

from .config import Config
from .machine import Action, Event

ORDER = ("Explore", "Debate", "DesignAudit", "Implement", "CodeAudit", "Merge")
ROUND_ROLE = {"Explore": "explorer", "Debate": "debater", "DesignAudit": "auditor", "Implement": "implementer", "CodeAudit": "auditor", "Merge": "driver"}
BOARD_STAGE = {"CodeAudit": "Audit", "Merge": "Deliver"}  # the board's Stage options have no CodeAudit or Merge (board.STAGE_OPTIONS)
BUDGET_KEY = {"Explore": "explore", "Debate": "debate", "DesignAudit": "design_audit", "Implement": "implement", "CodeAudit": "code_audit", "Merge": "merge"}
ACTION_KINDS = ("post", "notify_owner", "board_update")  # the arbiter emits nothing else: no delegate, set_driver, llm_call, stop_worker or arm_timer
DURABLE = ("post", "notify_owner")  # kinds the fold records in `Round.pending`: losing one loses the record, so they are re-emitted until the daemon takes them
SEEN_KEEP = 200  # message timestamps kept for the re-feed check

# A hand-off marker is required, so a plain progress post never clears a stage (design audit A3). The
# crew writes hand-offs both ways, "handing off" and "over to <holder>", so both forms count. The marker
# alone never clears: the author must hold the stage's role and the post must carry a link and a due time.
#
# "over to" and "passing it to" only count when they name who receives the work -- "you", a mention, or
# "the <role>". A bare "over to" is ordinary prose ("moving over to section C"), and letting it stand as a
# marker would let a holder's own progress post clear their stage, which is what A3 exists to prevent.
_RECIPIENT = r"(?:you\b|<@[UW][A-Z0-9]+>|(?:the|our)\s+(?:%s)\b)" % "|".join(sorted(set(ROUND_ROLE.values())))
HANDOFF_MARK = re.compile(r"hand[-\s]?off|hand(?:s|ed|ing)[-\s]+(?:it |this )?(?:off|over)|\ball yours\b"
                          rf"|\bover to\s+{_RECIPIENT}|\bpass(?:ing|es|ed)?[-\s]+(?:it|this)[-\s]+(?:on[-\s]+)?to\s+{_RECIPIENT}", re.I)
MENTION = re.compile(r"<@([UW][A-Z0-9]+)>")
LINK = re.compile(r"https://[^\s<>|)\]]+")
DUE = re.compile(r"\b(?:due|eta)\b[^\n]{0,40}?(?P<when>\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}\s*(?:ET|EST|EDT))", re.I)
SLACK_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")


class RosterError(ValueError):
    """An adoption the driver must refuse: an unknown role, a bad Slack id, the owner as a holder, or AI delegation of a person-held role."""


def round_key(thread: str) -> str: return f"round:{thread}"


# -- due and ETA text (New York time, `MM-DD HH:MM ET`) ---------------------------------
def _nth_sunday(year: int, month: int, n: int) -> dt.datetime:
    first = dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
    return first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def et_offset(ts: float) -> int:
    """Hours from UTC for New York, by the US rule in force since 2007 (EDT from the second Sunday in March to the first Sunday in November).

    Computed rather than read from a tz database, so the format is the same wherever the driver runs."""
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    start = _nth_sunday(d.year, 3, 2) + dt.timedelta(hours=7)  # 02:00 EST
    end = _nth_sunday(d.year, 11, 1) + dt.timedelta(hours=6)  # 02:00 EDT
    return -4 if start <= d < end else -5


def et_time(ts: float) -> str:
    return (dt.datetime.fromtimestamp(ts, dt.timezone.utc) + dt.timedelta(hours=et_offset(ts))).strftime("%m-%d %H:%M") + " ET"


def due_line(ts: float | None) -> str: return f"Due {et_time(ts)}" if ts else "Due (unset)"
def eta_line(ts: float | None) -> str: return f"ETA {et_time(ts)}" if ts else "ETA (unset)"
def day(ts: float | None) -> str | None: return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat() if ts else None


def budget(cfg: Config, stage: str) -> float:
    """The per-role crew budget for a stage, from `[crew_budget]`; a stage with no budget falls back to `stage_timeout`."""
    return float(cfg.crew_budget.get(BUDGET_KEY.get(stage, ""), cfg.stage_timeout))


def next_stage(rnd: "Round", stage: str) -> str | None:
    order = tuple(rnd.order) or ORDER
    i = order.index(stage) if stage in order else -1
    return order[i + 1] if 0 <= i < len(order) - 1 else None


@dataclass
class Round:
    """One adopted thread. Its own store namespace (`round:<thread>`), never in `studies`."""
    thread: str
    channel: str = ""
    issue: int | None = None
    repo: str = ""
    stage: str = ORDER[0]
    stage_started: float = 0.0
    adopted_at: float = 0.0
    due: float | None = None
    order: list[str] = field(default_factory=lambda: list(ORDER))
    roster: dict[str, dict] = field(default_factory=dict)  # role -> {holders: [slack ids], login, delegate}
    clearance: dict[str, dict] = field(default_factory=dict)  # stage -> {handoff_ts, author, link, due_text, mention}
    misses: dict[str, int] = field(default_factory=dict)  # "<stage>/<author>" -> hand-offs that lacked the mention
    findings: list[str] = field(default_factory=list)
    posted_refs: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)  # durable actions the daemon has not taken yet; cleared only on a successful send
    pr: dict = field(default_factory=dict)  # slice 2

    def holders(self, role: str) -> list[str]: return list((self.roster.get(role) or {}).get("holders") or [])
    def login(self, role: str, cfg: Config | None = None) -> str:
        entry = self.roster.get(role) or {}
        if entry.get("login"): return str(entry["login"])
        return next((cfg.login_of(h) for h in entry.get("holders") or [] if cfg and cfg.login_of(h)), "")

    def to_dict(self) -> dict: return {k: getattr(self, k) for k in self.__dataclass_fields__}
    def to_json(self) -> str: return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Round":
        """Tolerant of absent and unknown keys: a round written by an older or newer version still loads."""
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**{**{"thread": ""}, **known})

    @classmethod
    def from_json(cls, text: str) -> "Round": return cls.from_dict(json.loads(text))


# -- adoption (driver-only) -------------------------------------------------------------
def parse_roster(roles: list[str], logins: list[str], delegate: list[str], owner: str) -> dict[str, dict]:
    """`explorer=U1,U2` pairs into the roster, with `delegate` refused on every person-held role (R47.1)."""
    roster: dict[str, dict] = {}
    for item in roles or []:
        role, _, ids = str(item).partition("=")
        role = role.strip()
        if role not in ROUND_ROLE.values(): raise RosterError(f"unknown role {role!r}; roles are {', '.join(sorted(set(ROUND_ROLE.values())))}")
        holders = [h.strip() for h in ids.split(",") if h.strip()]
        if not holders: raise RosterError(f"role {role} has no holder: use --role {role}=U...")
        for h in holders:
            if not SLACK_ID.match(h): raise RosterError(f"{h!r} is not a Slack user id (U… or W…)")
            if h == owner: raise RosterError(f"the owner {owner} cannot hold {role}: the roster lists the crew, and the owner is escalated to, not assigned")
        roster.setdefault(role, {"holders": [], "login": "", "delegate": False})["holders"] += holders
    for item in logins or []:
        role, _, login = str(item).partition("=")
        if role.strip() not in roster: raise RosterError(f"--login {item}: {role.strip()!r} is not in the roster")
        roster[role.strip()]["login"] = login.strip()
    for role in delegate or []:
        held = ", ".join(roster.get(role, {}).get("holders") or [])
        raise RosterError(f"R47.1: {role} is held by {held}; an AI worker is never delegated for a person-held role" if held
                          else f"--delegate {role}: delegation is opt-in per role and is not executed in this slice")
    return roster


def adopt(thread: str, *, issue: int | None, repo: str, roles: list[str], logins: list[str] | None = None,
          delegate: list[str] | None = None, stage: str = ORDER[0], now: float, cfg: Config) -> tuple[Round, list[Action]]:
    """The driver adopts one thread. Keyed per thread, never per channel (the `issue:<channel>` leak at cli.py:38).

    Adoption writes no board field and posts nothing: the first clearance does that."""
    if len(str(thread).split(":")) != 3: raise RosterError(f"{thread!r} is not a thread id (workspace:channel:root_ts)")
    if stage not in ORDER: raise RosterError(f"unknown stage {stage!r}; stages are {', '.join(ORDER)}")
    roster = parse_roster(roles, logins or [], delegate or [], cfg.owner)
    if not roster: raise RosterError("a round needs a roster: --role explorer=U… --role debater=U… …")
    if not roster.get(ROUND_ROLE[stage], {}).get("holders"): raise RosterError(f"stage {stage} needs a {ROUND_ROLE[stage]}: --role {ROUND_ROLE[stage]}=U…")
    rnd = Round(thread=thread, channel=str(thread).split(":")[1], issue=int(issue) if issue else None, repo=repo or "",
                stage=stage, stage_started=now, adopted_at=now, due=now + budget(cfg, stage), roster=roster)
    return rnd, []


# -- the fold ---------------------------------------------------------------------------
def observe(rnd: Round, ev: Event, cfg: Config) -> tuple[Round, list[Action]]:
    """One thread event -> the next round and the actions the driver executes. Pure; `ev.now` is the only clock."""
    r = copy.deepcopy(rnd)
    if ev.kind != "crew_post": return r, []  # L3.1: a time event never advances, stops, closes, retries or delivers
    ts = str(ev.get("ts") or "")
    if ts and ts in r.seen: return r, []  # the same event again: never a second post, never a second advance
    if ts: r.seen = (r.seen + [ts])[-SEEN_KEEP:]
    text = str(ev.get("text") or "")
    sender = str(ev.get("sender") or "")
    if sender == cfg.owner or _ref_of(text) in r.posted_refs: return r, []  # the arbiter's own echo never clears a stage
    if not HANDOFF_MARK.search(text): return r, []  # a progress post is not a hand-off
    stage = r.stage
    nxt = next_stage(r, stage)
    if nxt is None: return r, []  # the merge stage closes the round; merging is a later slice
    role, nrole = ROUND_ROLE[stage], ROUND_ROLE[nxt]
    if sender not in r.holders(role):
        r.findings.append(f"{stage}: hand-off from {sender}, who does not hold {role}; stage not cleared")
        return r, []  # the off-role reminder is a later slice
    link = LINK.search(text)
    if not link:
        r.findings.append(f"{stage}: hand-off from {sender} carries no link; stage not cleared")
        return r, []
    due = DUE.search(text)
    if not due:
        r.findings.append(f"{stage}: hand-off from {sender} carries no due time (expected `Due MM-DD HH:MM ET`); stage not cleared")
        return r, []

    actions: list[Action] = []
    mentioned = set(MENTION.findall(text))
    next_holders = r.holders(nrole)
    has_mention = bool(mentioned & set(next_holders))
    rebased = ev.now + budget(cfg, nxt)
    if not has_mention and not next_holders:
        # Nobody holds the next role, so there is no mention to add: a post naming no one helps no one.
        # That is a gap in the roster the driver adopted with, not a miss by the hand-off's author.
        r.findings.append(f"{nxt}: no {nrole} in the roster, so the hand-off's missing mention cannot be added")
    elif not has_mention:  # G7a: the one fix, once per (stage, author), and the only mention the arbiter posts
        key = f"{stage}/{sender}"
        r.misses[key] = r.misses.get(key, 0) + 1
        ref = f"{r.thread}/round/{stage}/mention/{sender}"
        if ref not in r.posted_refs:
            r.posted_refs.append(ref)
            actions.append(Action("post", ref, {"thread": r.thread, "post_kind": "report", "text": mention_fix_text(r, stage, nxt, sender, link.group(0), rebased, ref)}))

    r.clearance[stage] = {"handoff_ts": ts, "author": sender, "link": link.group(0), "due_text": due.group("when"), "mention": has_mention}
    r.stage, r.stage_started, r.due = nxt, ev.now, rebased
    board = board_action(r, stage, nxt, nrole, ts, cfg)
    if board is not None: actions.append(board)
    # A5, restart half: what must reach the thread is recorded on the round before the driver sends anything,
    # so a crash between the save and the send, or a post the daemon refuses, is re-emitted rather than lost.
    r.pending = r.pending + [a.to_dict() for a in actions if a.kind in DURABLE]
    return r, actions


def mention_fix_text(r: Round, stage: str, nxt: str, author: str, link: str, due: float, ref: str) -> str:
    """The missing mention, added. Mention, link and due time only: no command, and nothing for the crew to run (G9)."""
    holders = " ".join(f"<@{h}>" for h in r.holders(ROUND_ROLE[nxt])) or "(holder unset)"
    return "\n".join([
        f"Hand-off from <@{author}> clears {stage}; it did not carry the next holder's mention, so this adds it.",
        f"{holders} holds {ROUND_ROLE[nxt]} for {nxt}.",
        f"Input: {link}",
        due_line(due),
        f"ref: {ref}",
    ])


def board_action(r: Round, stage: str, nxt: str, nrole: str, ts: str, cfg: Config) -> Action | None:
    """Field-level board writes only (Status, Stage, Role, dates, assignee): never `sync_study`, never an issue-body edit (#49)."""
    if not r.issue: return None
    assignee = r.login(nrole, cfg)
    previous = r.login(ROUND_ROLE[stage], cfg)
    return Action("board_update", f"{r.thread}/round/{stage}-{nxt}/board/{ts}", {
        "thread": r.thread, "issue": r.issue, "repo": r.repo,
        "options": {"Stage": BOARD_STAGE.get(nxt, nxt), "Role": nrole, "Status": "In Progress"},
        "dates": {"started": day(r.stage_started), "projected_finish": day(r.due)},
        "assignee": assignee,
        "remove_assignees": [previous] if previous and previous != assignee else [],
    })


def action_from_dict(d: dict) -> Action:
    """The inverse of `Action.to_dict` for a pending action read back from the round."""
    return Action(d["kind"], d["id"], {k: v for k, v in d.items() if k not in ("kind", "id")})


def _ref_of(text: str) -> str:
    m = re.search(r"^ref:\s*(\S+)\s*$", text or "", re.M)
    return m.group(1) if m else ""
