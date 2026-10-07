"""GitHub Projects v2 mirror (R3, R4, R8-R11): opt-in, driver-written, never blocking.

A thin client over `gh api graphql` with typed GraphQL variables (`$date: Date!`, `$v: Float!`):
the query and its variables go to gh as one JSON document on stdin (`--input -`), because
`gh -F` turns floats into strings and GraphQL then rejects them. No string-built mutations and
no `gh project` subcommands for values; `gh project field-create` only creates missing fields
once (there is no GraphQL mutation for that). `gh issue create|edit|close` manage the issues.

Projection (R9, R12, R13): one plain issue per study and one per stage run whose body names the
study (`Study: #N`; no sub-issue hierarchy), each a project item carrying Stage, Role, Status,
Started, Projected finish, Projected hours, Actual hours, Finished, Workers, Thread and exactly
one assignee. The audit stage is one card per audit scope: a peer reviewer's card is assigned to
that reviewer and closes on their SIGN-OFF line; the local auditor's card (only for scopes no
peer takes) closes with the stage. R14: the built-in Status follows the machine (Todo ahead of
the stage, In Progress with Started, Done with Finished). R10: the Roadmap view draws Started ->
Projected finish; its date fields cannot be set through the API (docs/protocol.md has the
one-time setup). `Reviewers` is reserved by Projects v2, hence `Peer reviewers`.
`sync` never raises into the driver: failures are logged and retried on the next transition.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .machine import State
from .roles import roles_table, holders

log = logging.getLogger("fridica_research.board")
Runner = Callable[[list[str], str | None], str]  # (argv, stdin) -> stdout
STAGE_OPTIONS = ("Explore", "Claim", "Debate", "DesignAudit", "Implement", "Audit", "Deliver", "Delivered", "Stopped")
ROLE_OPTIONS = ("explorer", "debater", "implementer", "auditor", "driver", "peer-reviewer")
STATUS = {"todo": "Todo", "in_progress": "In Progress", "done": "Done"}  # the built-in Status field's default options
OPTIONS = {"Stage": STAGE_OPTIONS, "Role": ROLE_OPTIONS}
FIELDS = {
    "Stage": "SINGLE_SELECT", "Role": "SINGLE_SELECT", "Iteration": "NUMBER", "Generation": "NUMBER", "Projected hours": "NUMBER", "Actual hours": "NUMBER",
    "Started": "DATE", "Projected finish": "DATE", "Finished": "DATE",
    "Owner": "TEXT", "Approach": "TEXT", "Workers": "TEXT", "Result": "TEXT", "Thread": "TEXT", "Follow-on": "TEXT", "Peer reviewers": "TEXT",
}
BOARD_STAGE = {"Blocked": "Stopped"}

_FIELD_NODES = "fields(first:100){nodes{... on ProjectV2Field{id name dataType} ... on ProjectV2SingleSelectField{id name dataType options{id name color description}} ... on ProjectV2IterationField{id name dataType}}}"
Q_DISCOVER = {kind: "query($owner:String!,$number:Int!){%s(login:$owner){projectV2(number:$number){id %s}}}" % (kind, _FIELD_NODES) for kind in ("user", "organization")}
Q_ITEMS = {kind: "query($owner:String!,$number:Int!,$after:String){%s(login:$owner){projectV2(number:$number){items(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{id content{... on Issue{number repository{name}}}}}}}}" % kind for kind in ("user", "organization")}
Q_ISSUE = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){id}}}"
Q_VERIFY = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){projectItems(first:20){nodes{project{number} started:fieldValueByName(name:\"Started\"){... on ProjectV2ItemFieldDateValue{date}} projected_finish:fieldValueByName(name:\"Projected finish\"){... on ProjectV2ItemFieldDateValue{date}} finished:fieldValueByName(name:\"Finished\"){... on ProjectV2ItemFieldDateValue{date}} projected_hours:fieldValueByName(name:\"Projected hours\"){... on ProjectV2ItemFieldNumberValue{number}} actual_hours:fieldValueByName(name:\"Actual hours\"){... on ProjectV2ItemFieldNumberValue{number}} stage:fieldValueByName(name:\"Stage\"){... on ProjectV2ItemFieldSingleSelectValue{name}}}}}}}"
M_UPDATE_FIELD = "mutation($field:ID!,$options:[ProjectV2SingleSelectFieldOptionInput!]!){updateProjectV2Field(input:{fieldId:$field,singleSelectOptions:$options}){projectV2Field{... on ProjectV2SingleSelectField{id}}}}"
M_ADD_ITEM = "mutation($project:ID!,$content:ID!){addProjectV2ItemById(input:{projectId:$project,contentId:$content}){item{id}}}"
_SET = "mutation($project:ID!,$item:ID!,$field:ID!,$%s:%s){updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,value:{%s:$%s}}){projectV2Item{id}}}"
M_SET_DATE = _SET % ("date", "Date!", "date", "date")
M_SET_NUMBER = _SET % ("v", "Float!", "number", "v")
M_SET_TEXT = _SET % ("v", "String!", "text", "v")
M_SET_OPTION = _SET % ("v", "String!", "singleSelectOptionId", "v")


@dataclass(frozen=True)
class ProjectIds:
    id: str
    fields: dict[str, dict]  # name -> {id, dataType, options: {name: id}}

    def to_json(self) -> str: return json.dumps({"id": self.id, "fields": self.fields}, sort_keys=True)
    @classmethod
    def from_json(cls, text: str) -> "ProjectIds":
        d = json.loads(text)
        return cls(d["id"], d["fields"])


class MissingField(KeyError):
    pass


def subprocess_runner(token_env: str) -> Runner:
    def run(argv: list[str], stdin: str | None = None) -> str:
        env = dict(os.environ)
        if token_env and os.environ.get(token_env): env["GH_TOKEN"] = os.environ[token_env]
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, timeout=60)
        if p.returncode: raise RuntimeError(f"{' '.join(argv[:3])} failed: {p.stderr.strip()[:300]}")
        return p.stdout
    return run


def day(ts: float | None) -> str | None: return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat() if ts else None


class Projects:
    """The R11 client: discover, item lookup, typed field writes, verify. Caches through `remember`/`recall`."""

    def __init__(self, cfg: Config, runner: Runner | None = None, remember=lambda k, v: None, recall=lambda k: None):
        self.cfg, self.b = cfg, cfg.board
        self.run = runner or subprocess_runner(cfg.board.token_env)
        self.remember, self.recall = remember, recall
        self._project: ProjectIds | None = None
        self.field_setup_failed = False

    def graphql(self, query: str, **variables) -> dict:
        out = json.loads(self.run(["gh", "api", "graphql", "--input", "-"], json.dumps({"query": query, "variables": variables})))
        if out.get("errors"): raise RuntimeError(str(out["errors"])[:300])
        return out["data"]

    def owner_kind(self) -> str: return "organization" if self.b.owner_type == "organization" else "user"

    def discover(self, owner: str, number: int, refresh: bool = False) -> ProjectIds:
        key = f"board:project:{owner}/{number}"
        cached = None if refresh else self.recall(key)
        if cached: return ProjectIds.from_json(cached)
        p = self.graphql(Q_DISCOVER[self.owner_kind()], owner=owner, number=number)[self.owner_kind()]["projectV2"]
        ids = ProjectIds(p["id"], {f["name"]: {"id": f["id"], "dataType": f["dataType"], "options": {o["name"]: o["id"] for o in f.get("options", [])}, "option_details": [{"id": o["id"], "name": o["name"], "color": o.get("color", "GRAY"), "description": o.get("description", "")} for o in f.get("options", [])]} for f in p["fields"]["nodes"] if f})
        self.remember(key, ids.to_json())
        return ids

    def project(self) -> ProjectIds:
        if self._project is None: self._project = self.discover(self.b.owner, self.b.number)
        return self._project

    def ensure_fields(self):
        """Create the fields of FIELDS that the project lacks (once); refresh the cache afterwards."""
        missing = [n for n in FIELDS if n not in self.project().fields]
        for name in missing:
            argv = ["gh", "project", "field-create", str(self.b.number), "--owner", self.b.owner, "--name", name, "--data-type", FIELDS[name]]
            if FIELDS[name] == "SINGLE_SELECT": argv += ["--single-select-options", ",".join(OPTIONS[name])]
            self.run(argv, None)
        if missing: self._project = self.discover(self.b.owner, self.b.number, refresh=True)
        stage = self.project().fields.get("Stage")
        if stage and "DesignAudit" not in stage["options"]:
            # Refresh an old cached field before preserving option colours and descriptions.
            self._project = self.discover(self.b.owner, self.b.number, refresh=True)
            stage = self.project().fields["Stage"]
            if "DesignAudit" not in stage["options"]:
                options = stage["option_details"] + [{"name": "DesignAudit", "color": "PURPLE", "description": "Consensus design audit before implementation"}]
                self.graphql(M_UPDATE_FIELD, field=stage["id"], options=options)
                self._project = self.discover(self.b.owner, self.b.number, refresh=True)

    def field(self, name: str) -> dict:
        f = self.project().fields.get(name)
        if f is None:
            self._project = self.discover(self.b.owner, self.b.number, refresh=True)
            f = self.project().fields.get(name)
        if f is None: raise MissingField(name)
        return f

    def item_for_issue(self, repo: str, issue_number: int) -> str:
        key = f"board:item:{repo}#{issue_number}"
        cached = self.recall(key)
        if cached: return cached
        name = repo.split("/", 1)[1]
        after = None
        while True:
            page = self.graphql(Q_ITEMS[self.owner_kind()], owner=self.b.owner, number=self.b.number, after=after)[self.owner_kind()]["projectV2"]["items"]
            for n in page["nodes"]:
                c = n.get("content") or {}
                if c.get("number") == issue_number and (c.get("repository") or {}).get("name") == name:
                    self.remember(key, n["id"])
                    return n["id"]
            if not page["pageInfo"]["hasNextPage"]: raise KeyError(f"{repo}#{issue_number} is not on project {self.b.number}")
            after = page["pageInfo"]["endCursor"]

    def _set(self, mutation: str, issue: int, name: str, **value):
        f = self.field(name)
        self.graphql(mutation, project=self.project().id, item=self.item_for_issue(self.b.repo, issue), field=f["id"], **value)

    def set_dates(self, issue: int, started: str | None = None, projected_finish: str | None = None, finished: str | None = None):
        for name, value in (("Started", started), ("Projected finish", projected_finish), ("Finished", finished)):
            if value: self._set(M_SET_DATE, issue, name, date=value)

    def set_number(self, issue: int, field: str, value: float): self._set(M_SET_NUMBER, issue, field, v=float(value))
    def set_text(self, issue: int, field: str, value: str): self._set(M_SET_TEXT, issue, field, v=str(value))
    def set_option(self, issue: int, field: str, option: str):
        options = self.field(field)["options"]
        # A refused migration must not invent a value or block writes to other fields.
        if option not in options and self.field_setup_failed: return False
        self._set(M_SET_OPTION, issue, field, v=options[option])

    def verify(self, issue: int) -> dict:
        owner, name = self.b.repo.split("/", 1)
        nodes = self.graphql(Q_VERIFY, owner=owner, name=name, number=issue)["repository"]["issue"]["projectItems"]["nodes"]
        item = next((n for n in nodes if (n.get("project") or {}).get("number") == self.b.number), None)
        if item is None: return {}
        pick = lambda n, k: (n or {}).get(k)  # noqa: E731
        return {"started": pick(item["started"], "date"), "projected_finish": pick(item["projected_finish"], "date"), "finished": pick(item["finished"], "date"), "projected_hours": pick(item["projected_hours"], "number"), "actual_hours": pick(item["actual_hours"], "number"), "stage": pick(item["stage"], "name")}

    # -- issues ------------------------------------------------------------------
    def create_issue(self, title: str, body: str, assignee: str = "") -> int:
        argv = ["gh", "issue", "create", "-R", self.b.repo, "--title", title, "--body", body]
        if assignee: argv += ["--assignee", assignee]
        url = self.run(argv, None).strip().splitlines()[-1]
        return int(url.rstrip("/").rsplit("/", 1)[1])

    def issue_id(self, number: int) -> str:
        owner, name = self.b.repo.split("/", 1)
        return self.graphql(Q_ISSUE, owner=owner, name=name, number=number)["repository"]["issue"]["id"]

    def add_to_project(self, issue: int) -> str:
        item = self.graphql(M_ADD_ITEM, project=self.project().id, content=self.issue_id(issue))["addProjectV2ItemById"]["item"]["id"]
        self.remember(f"board:item:{self.b.repo}#{issue}", item)
        return item

    def set_status(self, issue: int, key: str): self.set_option(issue, "Status", STATUS[key])  # R14: Status is built in, discovered like any field
    def edit_issue(self, number: int, *args: str): self.run(["gh", "issue", "edit", str(number), "-R", self.b.repo, *args], None)
    def close_issue(self, number: int): self.run(["gh", "issue", "close", str(number), "-R", self.b.repo], None)


# --- the study projection ----------------------------------------------------

def stage_table(state: State) -> str:
    rows = ["| Stage | Start | Projected | End | Actual |", "|---|---|---|---|---|"]
    for r in state.stage_log: rows.append(f"| {r['stage']} | {_hm(r['start'])} | {_dur(r['projected'])} | {_hm(r['end'])} | {_dur(r['actual'])} |")
    return "\n".join([f"Thread: {state.thread}", f"Generation {state.generation}, iteration {state.iteration}. Projected {state.projected_hours:g} h.", "", *rows])


def _hm(ts): return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else ""
def _dur(s): return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}" if s is not None else ""
def _workers(state: State, cfg: Config | None = None) -> str:
    text = ", ".join(f"{r}={w['worker_id']}" for r, w in sorted(state.workers.items()))
    return (roles_table(holders(state, cfg)) + "\n" + text) if cfg else text


def role_totals(state: State) -> dict[str, dict[str, float]]:
    """R12: projected and actual hours per role for one study (hours, from the stage log and the audit scopes)."""
    out: dict[str, dict[str, float]] = {}
    def add(role, projected, actual):
        r = out.setdefault(role, {"projected": 0.0, "actual": 0.0})
        r["projected"] += projected / 3600
        r["actual"] += (actual or 0) / 3600
    for row in state.stage_log:
        if row["stage"] != "Audit":
            add(row.get("role", "driver"), row["projected"], row["actual"])
            continue
        scopes = Board.scopes_of(state, row)  # each Audit row pairs with its own iteration's scopes and sign-off times
        for sc in scopes.values():
            if sc["reviewer"]: add("peer-reviewer", row["projected"], (sc["signed_at"] - row["start"]) if sc["signed_at"] else None)
            else: add("auditor", row["projected"], row["actual"])
        if not scopes: add("auditor", row["projected"], row["actual"])
    return {k: {m: round(v, 2) for m, v in r.items()} for k, r in out.items()}


class Board:
    """Mirrors a study onto the project: one plain issue per study, one per stage run, one per audit scope (R9, R13)."""

    def __init__(self, cfg: Config, runner: Runner | None = None, remember=lambda k, v: None, recall=lambda k: None, issue_numbers: dict | None = None):
        self.cfg, self.b = cfg, cfg.board
        self.api = Projects(cfg, runner, remember, recall)
        self.remember, self.recall = remember, recall
        self.issue_numbers = issue_numbers or {}  # thread/channel -> issue number given by `start --issue N`

    def login(self, slack_id: str, state: State) -> str: return self.cfg.login_of(slack_id, state.people)
    def title(self, state: State) -> str: return state.problem.strip().splitlines()[0][:80] if state.problem.strip() else state.thread

    def sync(self, state: State):
        """Return new setup findings for the driver; persist deduplication with the cards."""
        if not self.b.enabled: return []
        key = f"board:{state.thread}"
        cards = json.loads(self.recall(key) or "{}") or {"stages": []}
        findings = []
        try:
            self.api.field_setup_failed = False
            try:
                self.api.ensure_fields()
            except Exception as error:  # noqa: BLE001 - setup failures must not suppress available writes
                self.api.field_setup_failed = True
                if not cards.get("field_setup_finding"):
                    finding = "Board field setup failed; available fields continue syncing. Owner permission/setup action required."
                    cards["field_setup_finding"] = finding
                    findings.append(finding)
                    log.warning("board field setup failed for %s: %s", state.thread, error)
            self.sync_study(state, cards)
            self.sync_stages(state, cards)
            if state.stage in ("Delivered", "Stopped") and not cards.get("closed"):
                self.close(cards["issue"], day(state.finished_at or state.stage_log[-1]["end"]), ((state.finished_at or state.stage_log[-1]["end"] or state.started_at) - state.started_at) / 3600)
                cards["closed"] = True
        except Exception as e:  # noqa: BLE001 - the board must never stall a stage
            log.warning("board update failed for %s: %s", state.thread, e)
        finally:
            self.remember(key, json.dumps(cards))
        return findings

    def open_card(self, cards: list, scope: str | None, title: str, body: str, assignee: str, role: str, stage: str, start: float, projected: float, state: State) -> dict:
        """A plain issue with exactly one assignee (or none, never the owner on a peer's card), In Progress from creation.

        The issue number is recorded in `cards` right after `gh issue create`, before any field write: a transient
        gh failure then makes the next sync finish the writes (`filled`) instead of opening a second issue."""
        card = next((c for c in cards if c["scope"] == scope), None)
        if card is None:
            card = {"issue": self.api.create_issue(title, body, assignee), "closed": False, "scope": scope, "assignee": assignee, "filled": False}
            cards.append(card)
        if not card.get("filled", True):
            card["filled"] = self.fill_card(card["issue"], role, stage, start, projected, state)
        return card

    def fill_card(self, number: int, role: str, stage: str, start: float, projected: float, state: State):
        """The project item and its creation-time fields; every write is idempotent, so a retry repeats them all."""
        self.api.add_to_project(number)
        selected = [self.api.set_option(number, "Stage", stage), self.api.set_option(number, "Role", role), self.api.set_status(number, "in_progress")]
        self.api.set_dates(number, started=day(start), projected_finish=day(start + projected))
        self.api.set_number(number, "Projected hours", round(projected / 3600, 2))
        self.api.set_number(number, "Iteration", state.iteration)
        self.api.set_number(number, "Generation", state.generation)
        self.api.set_text(number, "Thread", state.thread)
        return all(result is not False for result in selected)

    def close(self, number: int, finished: str | None, actual_hours: float):
        self.api.set_dates(number, finished=finished)
        self.api.set_number(number, "Actual hours", round(actual_hours, 2))
        self.api.set_status(number, "done")
        self.api.close_issue(number)

    def sync_study(self, state: State, cards: dict):
        owner_login = self.login(self.cfg.owner, state)
        body = stage_table(state) + "\n\n" + roles_table(holders(state, self.cfg))
        if cards.get("field_setup_finding"): body += "\n\nFinding: " + cards["field_setup_finding"]
        misses = {k: v for k, v in state.mention_misses.items() if v >= 2}
        if misses: body += "\n\nRepeated handoff mention misses: " + json.dumps(misses, sort_keys=True)
        if "issue" not in cards:
            given = self.issue_numbers.get(state.thread) or self.issue_numbers.get(state.channel)
            cards["issue"], cards["given"], cards["filled"] = (int(given) if given else self.api.create_issue(self.title(state), body, owner_login)), bool(given), False
        n = cards["issue"]
        if not cards.get("filled", True):  # creation-time writes, finished on a later sync if gh failed midway
            if cards.get("given") and owner_login: self.api.edit_issue(n, "--add-assignee", owner_login)
            cards["filled"] = self.fill_card(n, "driver", "Explore", state.started_at, state.projected_hours * 3600, state)
            if owner_login: self.api.set_text(n, "Owner", owner_login)
        self.api.edit_issue(n, "--body", body)
        self.api.set_option(n, "Stage", BOARD_STAGE.get(state.stage, state.stage))
        self.api.set_number(n, "Iteration", state.iteration)
        for field, value in (("Approach", (state.claim or {}).get("slug")), ("Workers", _workers(state, self.cfg)), ("Result", state.notes[-1] if state.stage in ("Stopped", "Blocked") and state.notes else state.deliverable.get("summary", "")[:500]), ("Follow-on", state.followon), ("Peer reviewers", ", ".join(filter(None, (self.login(r.handle, state) for r in self.cfg.reviewers))))):
            if value: self.api.set_text(n, field, value)

    def audit_cards(self, state: State, row: dict, cards: dict, out: list):
        """R13: one card per audit scope; peers' cards go to the peer (never to the owner), the local auditor's to the owner."""
        iteration = row.get("iteration", state.iteration)
        for scope, sc in self.scopes_of(state, row).items():
            if sc["reviewer"]:
                self.open_card(out, scope, f"Audit {scope} (iteration {iteration}): {self.title(state)}"[:120], f"Study: #{cards['issue']}\nAudit scope: {scope}. Reply `SIGN-OFF <pr> <sha> approve|changes` in the study thread.", self.login(sc["reviewer"], state), "peer-reviewer", "Audit", row["start"], row["projected"], state)
            else:
                self.open_card(out, scope, f"Audit {scope} (iteration {iteration}, local auditor): {self.title(state)}"[:120], f"Study: #{cards['issue']}\nAudit scope: {scope}, by the owner's auditor worker.", self.login(self.cfg.owner, state), "auditor", "Audit", row["start"], row["projected"], state)

    @staticmethod
    def scopes_of(state: State, row: dict) -> dict:
        """The audit scopes of one Audit row: the row's own copy (mirrored by the machine), else the study's current scopes for the latest row."""
        if "scopes" in row: return row["scopes"]
        return state.audit_scopes if row is next((r for r in reversed(state.stage_log) if r["stage"] == "Audit"), None) else {}

    def sync_stages(self, state: State, cards: dict):
        owner_login = self.login(self.cfg.owner, state)
        stages = cards.setdefault("stages", [])
        for i, row in enumerate(state.stage_log):
            if i >= len(stages): stages.append({"cards": []})
            if row["stage"] == "Audit": self.audit_cards(state, row, cards, stages[i]["cards"])
            else: self.open_card(stages[i]["cards"], None, f"{row['stage']} (iteration {row.get('iteration', state.iteration)}): {self.title(state)}"[:120], f"Study: #{cards['issue']}\nStage run {i + 1} of {state.thread}.", owner_login, row.get("role", "driver"), row["stage"], row["start"], row["projected"], state)
            for card in stages[i]["cards"]:
                if card["closed"] or not card.get("filled", True): continue
                sc = self.scopes_of(state, row).get(card["scope"]) if card["scope"] else None
                if sc and sc["reviewer"]:
                    if not card["assignee"] and self.login(sc["reviewer"], state):
                        card["assignee"] = self.login(sc["reviewer"], state)
                        self.api.edit_issue(card["issue"], "--add-assignee", card["assignee"])
                    if sc["signed_at"] is None: continue  # closes on the assignee's SIGN-OFF line, not before
                    self.close(card["issue"], day(sc["signed_at"]), (sc["signed_at"] - row["start"]) / 3600)
                elif row["end"] is not None:
                    if _workers(state) and not card["scope"]: self.api.set_text(card["issue"], "Workers", _workers(state, self.cfg))
                    self.close(card["issue"], day(row["end"]), row["actual"] / 3600)
                else: continue
                card["closed"] = True
