"""#49: `sync_study` never writes an adopted issue's body (the maintainer's close criteria); a created issue keeps its stage table."""
from __future__ import annotations

import json

from fridica_research import board
from fridica_research.roles import holders, roles_table
import test_board as tb
from support import EXPLORER_REPORT, World, result

MAINTAINER_BODY = "Close when the tape replays D0.\n\n- [ ] criterion one\n- [ ] criterion two\n"


def bodies(gh: tb.FakeGh, seed: dict[int, str]) -> dict[int, str]:
    """Each issue's body as GitHub would hold it after the recorded `gh issue create/edit --body` calls."""
    out, created = dict(seed), 0
    for a in gh.argv("gh", "issue"):
        if a[2] == "create": created += 1
        if "--body" in a and a[2] in ("create", "edit"): out[created if a[2] == "create" else int(a[3])] = a[a.index("--body") + 1]
    return out


def three_syncs(adopt: bool):
    """Explore, Claim, Debate: three syncs across stage changes, with the issue adopted through `start --issue 42` or created."""
    b, gh, meta = tb.make()
    w = World(cfg=tb.BCFG)
    w.start()
    if adopt: meta["issue:C1"] = "42"
    b.sync(w.state)
    w.finish("explorer", result(report=EXPLORER_REPORT))
    b.sync(w.state)
    w.tick(w.cfg.settle_window)
    b.sync(w.state)
    assert w.state.stage == "Debate"
    return b, gh, meta, w


def test_adopted_issue_body_is_byte_identical_after_three_syncs():
    _, gh, meta, w = three_syncs(adopt=True)
    assert json.loads(meta[f"board:{w.state.thread}"])["issue"] == 42
    assert bodies(gh, {42: MAINTAINER_BODY})[42] == MAINTAINER_BODY
    assert not [a for a in gh.argv("gh", "issue", "edit", "42") if "--body" in a]
    # stage and iteration still reach the adopted card as board fields
    stage = [v["v"] for v in gh.sets(board.M_SET_OPTION) if v["item"] == "PVTI_I_42" and v["field"] == "F_stage"]
    assert stage[0] == "O_Explore" and stage[-1] == "O_Debate"
    assert any(v["item"] == "PVTI_I_42" and v["field"] == "F_Iteration" for v in gh.sets(board.M_SET_NUMBER))


def test_created_issue_still_gets_the_stage_table_on_every_sync():
    b, gh, meta, w = three_syncs(adopt=False)
    n = json.loads(meta[f"board:{w.state.thread}"])["issue"]
    assert n == 1
    edits = [a for a in gh.argv("gh", "issue", "edit", str(n)) if "--body" in a]
    assert len(edits) == 3
    assert bodies(gh, {})[n] == board.stage_table(w.state) + "\n\n" + roles_table(holders(w.state, b.cfg))
    assert "| Stage | Start | Projected | End | Actual |" in bodies(gh, {})[n]
