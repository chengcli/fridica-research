"""Board tests with a fake `gh` runner: exact queries, typed variables, the R9 card sequence, and failure isolation."""
from __future__ import annotations

import dataclasses
import json

import pytest

from fridica_research import board
from fridica_research.board import Board, Projects
from fridica_research.config import Board as BoardCfg
from support import CFG, EXPLORER_REPORT, PR, SHA, World, report, result

BCFG = dataclasses.replace(CFG, board=BoardCfg(enabled=True, owner="chengcli", number=8, repo="chengcli/fridica-research"))
PROJECT = {"id": "PVT_1", "fields": {"nodes": [
    {"id": "F_stage", "name": "Stage", "dataType": "SINGLE_SELECT", "options": [{"id": f"O_{n}", "name": n} for n in board.STAGE_OPTIONS]},
    {"id": "F_role", "name": "Role", "dataType": "SINGLE_SELECT", "options": [{"id": f"R_{n}", "name": n} for n in board.ROLE_OPTIONS]},
    {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT", "options": [{"id": f"S_{n}", "name": n} for n in ("Todo", "In Progress", "Done")]},
    *[{"id": f"F_{n.replace(' ', '_')}", "name": n, "dataType": k} for n, k in board.FIELDS.items() if n not in ("Stage", "Role")],
]}}


class FakeGh:
    """Answers gh calls; records (argv, parsed stdin). Missing fields and failures are scriptable."""

    def __init__(self, fields_missing: tuple[str, ...] = (), fail_on: str | None = None):
        self.calls: list[tuple[list[str], dict | None]] = []
        self.issues = 0
        self.missing = set(fields_missing)
        self.fail_on = fail_on
        self.fail_once: str | None = None  # fails the first matching call, then clears

    def __call__(self, argv: list[str], stdin: str | None) -> str:
        doc = json.loads(stdin) if stdin else None
        self.calls.append((argv, doc))
        if self.fail_on and self.fail_on in " ".join(argv) + (doc or {}).get("query", ""): raise RuntimeError("gh failed")
        if self.fail_once and self.fail_once in " ".join(argv) + (doc or {}).get("query", ""):
            self.fail_once = None
            raise RuntimeError("gh failed once")
        if argv[:3] == ["gh", "api", "graphql"]:
            return json.dumps({"data": self.answer(doc["query"], doc["variables"])})
        if argv[:3] == ["gh", "issue", "create"]:
            self.issues += 1
            return f"https://github.com/{argv[4]}/issues/{self.issues}\n"
        if argv[:3] == ["gh", "project", "field-create"]:
            self.missing.discard(argv[argv.index("--name") + 1])
            return "{}"
        return ""

    def answer(self, query: str, v: dict) -> dict:
        if query == board.Q_DISCOVER["user"]:
            nodes = [f for f in PROJECT["fields"]["nodes"] if f["name"] not in self.missing]
            return {"user": {"projectV2": {"id": PROJECT["id"], "fields": {"nodes": nodes}}}}
        if query == board.Q_ISSUE: return {"repository": {"issue": {"id": f"I_{v['number']}"}}}
        if query == board.M_ADD_ITEM: return {"addProjectV2ItemById": {"item": {"id": "PVTI_" + v["content"]}}}
        if query in (board.M_SET_DATE, board.M_SET_NUMBER, board.M_SET_TEXT, board.M_SET_OPTION): return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": v["item"]}}}
        if query == board.Q_ITEMS["user"]: return {"user": {"projectV2": {"items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": "PVTI_I_3", "content": {"number": 3, "repository": {"name": "fridica-research"}}}]}}}}
        if query == board.Q_VERIFY: return {"repository": {"issue": {"projectItems": {"nodes": [{"project": {"number": 8}, "started": {"date": "2026-10-04"}, "projected_finish": {"date": "2026-10-04"}, "finished": None, "projected_hours": {"number": 4.0}, "actual_hours": None, "stage": {"name": "Explore"}}]}}}}
        raise AssertionError(f"unexpected query {query[:60]}")

    def sets(self, mutation: str) -> list[dict]: return [doc["variables"] for argv, doc in self.calls if doc and doc["query"] == mutation]
    def argv(self, *prefix: str) -> list[list[str]]: return [a for a, _ in self.calls if tuple(a[: len(prefix)]) == prefix]


def make(gh: FakeGh | None = None, cfg=BCFG):
    gh = gh or FakeGh()
    meta: dict[str, str] = {}
    b = Board(cfg, runner=gh, remember=lambda k, v: meta.__setitem__(k, v) if v is not None else meta.pop(k, None), recall=meta.get)
    return b, gh, meta


def test_discover_is_one_query_and_cached():
    b, gh, meta = make()
    ids = b.api.discover("chengcli", 8)
    assert gh.calls[0][0] == ["gh", "api", "graphql", "--input", "-"]
    assert gh.calls[0][1] == {"query": board.Q_DISCOVER["user"], "variables": {"owner": "chengcli", "number": 8}}
    assert ids.id == "PVT_1" and ids.fields["Stage"]["options"]["Explore"] == "O_Explore" and ids.fields["Started"]["dataType"] == "DATE"
    assert ids.fields["Role"]["options"]["peer-reviewer"] == "R_peer-reviewer" and ids.fields["Status"]["options"]["In Progress"] == "S_In Progress"
    assert "board:project:chengcli/8" in meta
    b.api.discover("chengcli", 8)
    assert len(gh.calls) == 1


def test_typed_field_writes_and_item_lookup():
    b, gh, meta = make()
    b.api.set_dates(3, started="2026-10-04", projected_finish="2026-10-05")
    b.api.set_number(3, "Actual hours", 0.17)
    b.api.set_text(3, "Thread", "T1:C1:1")
    b.api.set_option(3, "Stage", "Debate")
    items = [d for _, d in gh.calls if d and d["query"] == board.Q_ITEMS["user"]]
    assert len(items) == 1 and items[0]["variables"] == {"owner": "chengcli", "number": 8, "after": None}  # cached after the first lookup
    assert gh.sets(board.M_SET_DATE) == [{"project": "PVT_1", "item": "PVTI_I_3", "field": "F_Started", "date": "2026-10-04"}, {"project": "PVT_1", "item": "PVTI_I_3", "field": "F_Projected_finish", "date": "2026-10-05"}]
    assert gh.sets(board.M_SET_NUMBER) == [{"project": "PVT_1", "item": "PVTI_I_3", "field": "F_Actual_hours", "v": 0.17}]
    assert isinstance(gh.sets(board.M_SET_NUMBER)[0]["v"], float)
    assert "$v:Float!" in board.M_SET_NUMBER and "$date:Date!" in board.M_SET_DATE
    assert gh.sets(board.M_SET_TEXT) == [{"project": "PVT_1", "item": "PVTI_I_3", "field": "F_Thread", "v": "T1:C1:1"}]
    assert gh.sets(board.M_SET_OPTION) == [{"project": "PVT_1", "item": "PVTI_I_3", "field": "F_stage", "v": "O_Debate"}]
    assert meta["board:item:chengcli/fridica-research#3"] == "PVTI_I_3"


def test_verify_reads_field_values_by_name():
    b, gh, _ = make()
    v = b.api.verify(3)
    assert gh.calls[-1][1]["variables"] == {"owner": "chengcli", "name": "fridica-research", "number": 3}
    assert v == {"started": "2026-10-04", "projected_finish": "2026-10-04", "finished": None, "projected_hours": 4.0, "actual_hours": None, "stage": "Explore"}


def test_missing_fields_are_created_once():
    gh = FakeGh(fields_missing=("Peer reviewers", "Finished", "Role"))
    b, gh, _ = make(gh)
    b.api.ensure_fields()
    creates = gh.argv("gh", "project", "field-create")
    assert [c[c.index("--name") + 1] for c in creates] == ["Role", "Finished", "Peer reviewers"]
    assert creates[0][creates[0].index("--single-select-options") + 1] == "explorer,debater,implementer,auditor,driver,peer-reviewer"
    assert creates[1][creates[1].index("--data-type") + 1] == "DATE"
    assert "Peer reviewers" in b.api.project().fields
    b.api.ensure_fields()
    assert len(gh.argv("gh", "project", "field-create")) == 3


def cards_of(meta, w): return json.loads(meta[f"board:{w.state.thread}"])
def flat(cards): return [c for st in cards["stages"] for c in st["cards"]]


def test_study_and_stage_cards_are_plain_issues_with_one_assignee():
    """R9/R13: study issue #1, then one plain issue per stage run naming `Study: #1`; closed with Finished, Actual hours, Status Done."""
    w = World(cfg=BCFG)
    b, gh, meta = make()
    w.start()
    b.sync(w.state)
    creates = gh.argv("gh", "issue", "create")
    assert len(creates) == 2 and creates[0][6] == "Study the thing" and creates[1][6].startswith("Explore (iteration 1)")
    assert creates[1][8].startswith("Study: #1\n") and all(c.count("--assignee") == 1 and c[c.index("--assignee") + 1] == "chengcli" for c in creates)
    assert not any("addSubIssue" in (d or {}).get("query", "") for _, d in gh.calls)
    assert [v["content"] for v in gh.sets(board.M_ADD_ITEM)] == ["I_1", "I_2"]
    dates = {(v["item"], v["field"]): v["date"] for v in gh.sets(board.M_SET_DATE)}
    assert dates[("PVTI_I_1", "F_Started")] == "2023-11-14" and ("PVTI_I_1", "F_Projected_finish") in dates and ("PVTI_I_2", "F_Started") in dates
    roles = {v["item"]: v["v"] for v in gh.sets(board.M_SET_OPTION) if v["field"] == "F_role"}
    assert roles == {"PVTI_I_1": "R_driver", "PVTI_I_2": "R_explorer"}
    status = [(v["item"], v["v"]) for v in gh.sets(board.M_SET_OPTION) if v["field"] == "F_status"]
    assert status == [("PVTI_I_1", "S_In Progress"), ("PVTI_I_2", "S_In Progress")]  # R14
    w.to_delivered()
    b.sync(w.state)
    cards = cards_of(meta, w)
    # Audit: one card per scope: the peer's (scope) and the local auditor's (code).
    assert cards["issue"] == 1 and [[c["issue"] for c in st["cards"]] for st in cards["stages"]] == [[2], [3], [4], [5], [6], [7, 8], [9]]
    titles = [a[6] for a in gh.argv("gh", "issue", "create")[1:]]
    assert [t.split(":")[0] for t in titles] == ["Explore (iteration 1)", "Claim (iteration 1)", "Debate (iteration 1)", "DesignAudit (iteration 1)", "Implement (iteration 1)", "Audit scope (iteration 1)", "Audit code (iteration 1, local auditor)", "Deliver (iteration 1)"]
    assigned = {int(a[6].split(":")[0].split()[-1].strip("()")) if False else i + 1: a[a.index("--assignee") + 1] for i, a in enumerate(gh.argv("gh", "issue", "create"))}
    assert assigned[7] == "reviewer" and assigned[8] == "chengcli"  # the peer's card is the peer's; the owner never assigns itself to it
    roles = {v["item"]: v["v"] for v in gh.sets(board.M_SET_OPTION) if v["field"] == "F_role"}
    assert roles["PVTI_I_7"] == "R_peer-reviewer" and roles["PVTI_I_8"] == "R_auditor" and roles["PVTI_I_4"] == "R_debater" and roles["PVTI_I_9"] == "R_driver"
    closed = [a[3] for a in gh.argv("gh", "issue", "close")]
    assert closed == ["2", "3", "4", "5", "6", "8", "9", "1"]  # #6 waits for the reviewer's SIGN-OFF
    assert not [c for c in flat(cards) if c["issue"] == 7][0]["closed"]
    done = [v["item"] for v in gh.sets(board.M_SET_OPTION) if v["field"] == "F_status" and v["v"] == "S_Done"]
    assert done == [f"PVTI_I_{n}" for n in (2, 3, 4, 5, 6, 8, 9, 1)]
    actual = [v["item"] for v in gh.sets(board.M_SET_NUMBER) if v["field"] == "F_Actual_hours"]
    assert sorted(actual) == sorted(done)
    options = [v["v"] for v in gh.sets(board.M_SET_OPTION) if v["item"] == "PVTI_I_1" and v["field"] == "F_stage"]
    assert options[0] == "O_Explore" and options[-1] == "O_Delivered"
    body_edits = [a for a in gh.argv("gh", "issue", "edit") if "--body" in a]
    assert "| Stage | Start | Projected | End | Actual |" in body_edits[-1][body_edits[-1].index("--body") + 1]
    assert any(v["field"] == "F_Peer_reviewers" and v["v"] == "reviewer" for v in gh.sets(board.M_SET_TEXT))
    # The reviewer signs off after delivery: their card closes with Finished = sign-off day and Status Done.
    w.now += 7200
    w.ev("sign_off", sender="UREV", pr=PR, sha=SHA, verdict="approve")
    b.sync(w.state)
    assert [a[3] for a in gh.argv("gh", "issue", "close")][-1] == "7" and [c for c in flat(cards_of(meta, w)) if c["issue"] == 7][0]["closed"]
    assert [v for v in gh.sets(board.M_SET_NUMBER) if v["item"] == "PVTI_I_7" and v["field"] == "F_Actual_hours"][0]["v"] == 2.0


def test_peer_card_unassigned_until_login_known_never_the_owner():
    cfg = dataclasses.replace(BCFG, people={"UOWNER": "chengcli"})  # the reviewer's login is unknown
    w = World(cfg=cfg)
    b, gh, meta = make(cfg=cfg)
    w.to_audit()
    b.sync(w.state)
    peer = [a for a in gh.argv("gh", "issue", "create") if a[6].startswith("Audit scope")][0]
    assert "--assignee" not in peer
    w.ev("login_reply", sender="UREV", login="late-login")
    b.sync(w.state)
    adds = [a for a in gh.argv("gh", "issue", "edit") if "--add-assignee" in a]
    assert len(adds) == 1 and adds[0][adds[0].index("--add-assignee") + 1] == "late-login"


def test_role_totals():
    w = World()
    w.to_delivered()
    w.now += 1800
    w.ev("sign_off", sender="UREV", pr=PR, sha=SHA, verdict="approve")
    totals = board.role_totals(w.state)
    assert set(totals) == {"explorer", "driver", "debater", "implementer", "auditor", "peer-reviewer"}
    assert totals["explorer"]["projected"] == 0.33 and totals["driver"]["projected"] == round((120 + 600) / 3600, 2)
    assert totals["peer-reviewer"] == {"projected": 1.5, "actual": 0.5} and totals["auditor"]["projected"] == 1.83


def test_existing_issue_is_attached_not_created():
    w = World(cfg=BCFG)
    b, gh, meta = make()
    meta[f"issue:{w.start().thread}"] = "42"
    b.sync(w.state)
    assert [a[6] for a in gh.argv("gh", "issue", "create")] == ["Explore (iteration 1): Study the thing"]
    assert gh.sets(board.M_ADD_ITEM)[0]["content"] == "I_42" and gh.argv("gh", "issue", "create")[0][8].startswith("Study: #42")
    assert json.loads(meta[f"board:{w.state.thread}"])["issue"] == 42


def test_board_failure_is_logged_and_retried_next_transition(caplog):
    w = World(cfg=BCFG)
    gh = FakeGh(fail_on="Explore (iteration 1)")
    b, gh, meta = make(gh)
    w.start()
    b.sync(w.state)  # the study card is created, the stage card's `gh issue create` fails
    assert "board update failed" in caplog.text
    cards = json.loads(meta[f"board:{w.state.thread}"])
    assert cards["issue"] == 1 and flat(cards) == []
    gh.fail_on = None
    b.sync(w.state)
    assert [c["issue"] for c in flat(json.loads(meta[f"board:{w.state.thread}"]))] == [2]


def test_disabled_board_does_nothing():
    w = World()
    b, gh, _ = make(cfg=CFG)
    b.sync(w.start())
    assert gh.calls == []


def test_stage_table_renders_wall_clock_rows():
    w = World()
    w.to_claim()
    t = board.stage_table(w.state)
    assert "| Explore | 2023-11-14 22:13 | 0:20 | 2023-11-14 22:13 | 0:00 |" in t and "| Claim |" in t


@pytest.mark.parametrize("name,kind", list(board.FIELDS.items()))
def test_field_catalog_matches_project_8(name, kind):
    assert kind in ("SINGLE_SELECT", "NUMBER", "DATE", "TEXT") and name != "Reviewers"


def test_projects_organization_owner_uses_organization_query():
    cfg = dataclasses.replace(BCFG, board=dataclasses.replace(BCFG.board, owner_type="organization"))
    calls = []
    def gh(argv, stdin):
        calls.append(json.loads(stdin))
        return json.dumps({"data": {"organization": {"projectV2": {"id": "PVT_o", "fields": {"nodes": []}}}}})
    assert Projects(cfg, gh).discover("org", 1).id == "PVT_o" and calls[0]["query"].startswith("query($owner:String!,$number:Int!){organization(login:$owner)")


def test_issue_number_is_saved_before_field_writes_so_a_retry_never_duplicates():
    """F1: `gh issue create` succeeded, the next write failed: the retry finishes the writes on the same issue."""
    w = World(cfg=BCFG)
    gh = FakeGh()
    b, gh, meta = make(gh)
    w.start()
    gh.fail_once = board.M_ADD_ITEM  # the study card's addProjectV2ItemById fails right after its creation
    b.sync(w.state)
    cards = cards_of(meta, w)
    assert cards["issue"] == 1 and cards["filled"] is False and cards["stages"] == [] and len(gh.argv("gh", "issue", "create")) == 1
    b.sync(w.state)
    cards = cards_of(meta, w)
    assert cards["filled"] is True and [c["issue"] for c in flat(cards)] == [2] and len(gh.argv("gh", "issue", "create")) == 2
    assert [v["content"] for v in gh.sets(board.M_ADD_ITEM)] == ["I_1", "I_1", "I_2"]  # the failed add, its retry, then #2
    w.to_claim()
    gh.fail_once = board.M_ADD_ITEM  # now the Claim stage card
    b.sync(w.state)
    assert [(c["issue"], c["filled"]) for c in flat(cards_of(meta, w))] == [(2, True), (3, False)]
    b.sync(w.state)
    assert [(c["issue"], c["filled"]) for c in flat(cards_of(meta, w))] == [(2, True), (3, True)] and len(gh.argv("gh", "issue", "create")) == 3
    assert [v["content"] for v in gh.sets(board.M_ADD_ITEM)].count("I_3") == 2  # the failed add and its retry, no second issue
    assert [v["v"] for v in gh.sets(board.M_SET_OPTION) if v["item"] == "PVTI_I_3" and v["field"] == "F_role"] == ["R_driver"]


def test_role_totals_pair_each_audit_row_with_its_own_iteration():
    """Z2: two iterations, a peer sign-off in each; peer time is the sum of the two latencies, not every row x every sign-off."""
    w = World()
    w.to_audit()
    w.now += 1800
    w.ev("sign_off", sender="UREV", pr=PR, sha=SHA, verdict="approve")
    w.finish("auditor", result(report=report(verdict="return")))
    assert w.state.iteration == 2
    w.finish("explorer", result(report=EXPLORER_REPORT))
    w.tick(CFG.settle_window)
    w.finish("mathematician", result(report=report(position="agree")))
    w.finish("physicist", result(report=report(position="agree")))
    if w.state.stage == "DesignAudit": w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=[PR], machine_state={"branch": "b", "commit": "def5678", "dirty": False}))
    w.now += 3600
    w.ev("sign_off", sender="UREV", pr=PR, sha="def5678", verdict="approve")
    w.finish("auditor", result(report=report(verdict="pass")))
    assert w.state.stage == "Delivered"
    audits = [r for r in w.state.stage_log if r["stage"] == "Audit"]
    assert [r["iteration"] for r in audits] == [1, 2] and [r["scopes"]["scope"]["signed_at"] - r["start"] for r in audits] == [1800, 3600]
    totals = board.role_totals(w.state)
    assert totals["peer-reviewer"] == {"projected": 3.0, "actual": 1.5} and totals["auditor"]["projected"] == 3.67


def test_old_iteration_peer_card_does_not_close_on_a_new_iteration_signoff():
    """The board pairs each Audit row's cards with that row's scopes, not the study's current ones."""
    w = World(cfg=BCFG)
    b, gh, meta = make()
    w.to_audit()
    w.finish("auditor", result(report=report(verdict="return")))  # iteration 1 ends with the peer's scope unsigned
    w.finish("explorer", result(report=EXPLORER_REPORT))
    w.tick(CFG.settle_window)
    w.finish("mathematician", result(report=report(position="agree")))
    w.finish("physicist", result(report=report(position="agree")))
    if w.state.stage == "DesignAudit": w.finish("auditor", result(report=report(verdict="pass")))
    w.finish("implementer", result(artifacts=[PR], machine_state={"branch": "b", "commit": "def5678", "dirty": False}))
    w.ev("sign_off", sender="UREV", pr=PR, sha="def5678", verdict="approve")
    b.sync(w.state)
    peer_cards = [c for c in flat(cards_of(meta, w)) if c["scope"] == "scope"]
    assert [(c["issue"], c["closed"]) for c in peer_cards] == [(7, False), (14, True)]  # #8-#11 are iteration 2's Explore..Implement


def test_design_stage_option_migration_preserves_options_and_runs_once():
    """Input fields and existing options captured from real GitHub; success response is selection-pinned."""
    from pathlib import Path
    captured = json.loads((Path(__file__).parent / "fixtures/stage_field.json").read_text())
    stage = captured["stage"]
    assert {f["name"] for f in captured["input_schema"]["data"]["__type"]["inputFields"]} == {"id", "name", "color", "description"}

    class OldProjectGh(FakeGh):
        def __init__(self):
            super().__init__()
            self.options = stage["options"].copy()

        def answer(self, query, variables):
            if "updateProjectV2Field(input:" in query:
                self.options = [dict(option, id=option.get("id", "NEW_OPTION")) for option in variables["options"]]
                return {"updateProjectV2Field": {"projectV2Field": {"id": stage["id"]}}}
            data = super().answer(query, variables)
            if query == board.Q_DISCOVER["user"]:
                nodes = data["user"]["projectV2"]["fields"]["nodes"]
                data["user"]["projectV2"]["fields"]["nodes"] = [dict(f, id=stage["id"], options=self.options) if f["name"] == "Stage" else f for f in nodes]
            return data

    gh = OldProjectGh()
    b, _, _ = make(gh)
    b.api.ensure_fields()
    assert "DesignAudit" in b.api.project().fields["Stage"]["options"]
    assert gh.options[:len(stage["options"])] == stage["options"]
    b.api.ensure_fields()
    assert len([d for _, d in gh.calls if d and "updateProjectV2Field(input:" in d["query"]]) == 1


def test_roles_table_in_protocol_issue_and_workers_field_and_repeated_misses():
    from pathlib import Path
    from fridica_research import roles
    w = World(cfg=BCFG)
    w.to_debate()
    w.state.mention_misses["Debate/author"] = 2
    b, gh, _ = make()
    b.sync(w.state)
    text = roles.roles_table(roles.holders(w.state, BCFG))
    assert roles.roles_table() in (Path(__file__).parents[1] / "docs/protocol.md").read_text()
    bodies = [a[a.index("--body") + 1] for a in gh.argv("gh", "issue", "edit") if "--body" in a]
    assert text in bodies[0] and '"Debate/author": 2' in bodies[0]
    workers = [v["v"] for v in gh.sets(board.M_SET_TEXT) if v["field"] == "F_Workers"]
    assert text in workers[0]
    assert any(v["field"] == "F_role" and v["v"] == "R_driver" for v in gh.sets(board.M_SET_OPTION))
