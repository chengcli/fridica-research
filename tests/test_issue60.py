"""#60: an adopted issue's one adoption comment carries what the body used to (the field-setup finding, the mention-miss
line, stage timing, provenance); its id is stored with the cards and the comment is PATCHed when its text changes.
The issue body is never written (#49). A legacy card (`commented`, no id) finds its comment by the marker: never a second one."""
from __future__ import annotations

import json

import test_board as tb
from support import EXPLORER_REPORT, World, result

SHA = "a" * 40
THREAD = "T1:C1:1700000000.000100"


def patches(gh: tb.FakeGh) -> list[list[str]]: return [a for a in gh.argv("gh", "api") if "PATCH" in a]
def posts(gh: tb.FakeGh) -> list[list[str]]: return gh.argv("gh", "issue", "comment")


def adopted_world(gh: tb.FakeGh):
    b, gh, meta = tb.make(gh)
    w = World(cfg=tb.BCFG)
    w.start(thread=THREAD, producer={"repo": "chengcli/fridica-research", "sha": SHA})
    meta["issue:C1"] = "42"
    return b, gh, meta, w


def test_adoption_comment_carries_the_old_body_content_and_is_patched_in_place():
    b, gh, meta, w = adopted_world(tb.FakeGh(fields_missing=("Result",), fail_on="field-create"))  # field setup fails: a finding
    w.state.mention_misses = {"Debate/U9": 2}
    b.sync(w.state)
    w.finish("explorer", result(report=EXPLORER_REPORT))
    b.sync(w.state)
    w.tick(w.cfg.settle_window)
    b.sync(w.state)
    assert w.state.stage == "Debate"
    cards = json.loads(meta[f"board:{THREAD}"])
    [comment] = gh.comments[42]  # one comment over three syncs
    assert len(posts(gh)) == 1 and cards["comment_id"] == comment["id"]
    text = comment["body"]
    assert f"Adopted as the study card of fridica-research study {THREAD}" in text
    assert "Finding: Board field setup failed" in text and "Owner permission/setup action required." in text
    assert 'Repeated handoff mention misses: {"Debate/U9": 2}' in text
    assert "| Stage | Start | Projected | End | Actual |" in text and "| Explore |" in text and "| Debate |" in text  # stage timing
    assert f'producer: {{"repo":"chengcli/fridica-research","sha":"{SHA}","tree":null}}' in text and "generation: 1" in text  # provenance
    # the text changed with each stage: PATCHes of the stored id, never a body edit
    assert patches(gh) and all(a[a.index("PATCH") + 1] == f"repos/chengcli/fridica-research/issues/comments/{comment['id']}" for a in patches(gh))
    assert not [a for a in gh.argv("gh", "issue", "edit") if "--body" in a]
    n = len(patches(gh))
    b.sync(w.state)  # unchanged text: no write at all
    assert len(patches(gh)) == n and len(posts(gh)) == 1
    w.state.mention_misses["Debate/U9"] = 3
    b.sync(w.state)  # changed text: one PATCH, no new comment
    assert len(patches(gh)) == n + 1 and len(posts(gh)) == 1 and len(gh.comments[42]) == 1
    assert '{"Debate/U9": 3}' in gh.comments[42][0]["body"]


def test_legacy_card_commented_without_id_finds_and_patches_its_comment():
    """Cards written before #60 have `commented` and no id: the comment is found by its marker, its id stored, then PATCHed.
    The marker comment is the 2nd of 3, so on page 1 of 2 (two per page); the page-2 case is in the 404 test below."""
    gh = tb.FakeGh()
    b, gh, meta, w = adopted_world(gh)
    gh.add_comment("chengcli/fridica-research", 42, "An unrelated remark.", "maintainer")
    old = gh.add_comment("chengcli/fridica-research", 42, f"Adopted as the study card of fridica-research study {THREAD} (generation 1); stage cards link here as `Study: #42`. The driver never closes this issue; the PR's `Closes #42` does.", gh.login)
    gh.add_comment("chengcli/fridica-research", 42, "Another remark, on page 2 (pages of two): the lookup reads every page.", "maintainer")
    meta.pop("issue:C1")
    meta[f"board:{THREAD}"] = json.dumps({"issue": 42, "given": True, "filled": False, "commented": True, "stages": []})
    b.sync(w.state)
    b.sync(w.state)
    assert posts(gh) == [] and len(gh.comments[42]) == 3
    assert json.loads(meta[f"board:{THREAD}"])["comment_id"] == old["id"]
    assert [a[a.index("PATCH") + 1] for a in patches(gh)] == [f"repos/chengcli/fridica-research/issues/comments/{old['id']}"]  # once: the second sync is unchanged
    assert "| Stage | Start | Projected | End | Actual |" in old["body"]


def test_legacy_card_whose_comment_is_gone_gets_exactly_one():
    b, gh, meta, w = adopted_world(tb.FakeGh())
    meta.pop("issue:C1")
    meta[f"board:{THREAD}"] = json.dumps({"issue": 42, "given": True, "filled": False, "commented": True, "stages": []})
    b.sync(w.state)
    b.sync(w.state)
    assert len(posts(gh)) == 1 and len(gh.comments[42]) == 1 and json.loads(meta[f"board:{THREAD}"])["comment_id"] == gh.comments[42][0]["id"]


def legacy_cards(meta: dict, **extra):
    meta.pop("issue:C1", None)
    meta[f"board:{THREAD}"] = json.dumps({"issue": 42, "given": True, "filled": False, "commented": True, "stages": [], **extra})


def not_found(gh: tb.FakeGh, cid: int) -> list[list[str]]: return [a for a in patches(gh) if a[a.index("PATCH") + 1].endswith(f"/issues/comments/{cid}")]


def test_deleted_adoption_comment_is_recreated_once_then_patched():
    """A PATCH 404 (the comment was deleted) drops the stale id, looks the marker up again and posts one new comment only
    because none is found; later syncs PATCH the new id, so the board keeps advancing and the 404 is not repeated."""
    b, gh, meta, w = adopted_world(tb.FakeGh())
    b.sync(w.state)
    [old] = gh.comments.pop(42)  # deleted on GitHub
    for k in range(3):
        w.state.mention_misses = {"Debate/U9": 2 + k}  # the text changes on every sync
        b.sync(w.state)
    cards = json.loads(meta[f"board:{THREAD}"])
    [new] = gh.comments[42]
    assert len(posts(gh)) == 2 and len(not_found(gh, old["id"])) == 1  # the original, one recreate; one 404
    assert cards["comment_id"] == new["id"] != old["id"] and '{"Debate/U9": 4}' in new["body"]
    assert len(not_found(gh, new["id"])) == 2  # the two later syncs PATCH the new id
    stage = [v for v in gh.sets(tb.board.M_SET_OPTION) if v["item"] == "PVTI_I_42" and v["field"] == "F_stage"]
    assert len(stage) == 5  # fill_card once, then the Stage write after the comment on each of the 4 syncs


def test_404_with_the_marker_comment_still_present_readopts_it():
    """The stored id is stale but the driver's adoption comment exists (here the 3rd comment, on page 2): it is re-adopted, nothing posted."""
    b, gh, meta, w = adopted_world(tb.FakeGh())
    for who in ("maintainer", "maintainer"): gh.add_comment("chengcli/fridica-research", 42, "A remark.", who)
    live = gh.add_comment("chengcli/fridica-research", 42, f"Adopted as the study card of fridica-research study {THREAD} (generation 1); stage cards link here as `Study: #42`.", gh.login)
    legacy_cards(meta, comment_id=12345, comment_sha="stale")
    b.sync(w.state)
    b.sync(w.state)
    assert posts(gh) == [] and len(not_found(gh, 12345)) == 1 and len(not_found(gh, live["id"])) == 1
    assert json.loads(meta[f"board:{THREAD}"])["comment_id"] == live["id"] and "| Stage |" in live["body"]


def test_marker_comment_by_another_login_is_ignored():
    """Only the driver's own login's comments count: a quoted marker by someone else is neither adopted nor PATCHed."""
    b, gh, meta, w = adopted_world(tb.FakeGh())
    quote = f"Adopted as the study card of fridica-research study {THREAD} (generation 1); quoted by a maintainer."
    other = gh.add_comment("chengcli/fridica-research", 42, quote, "maintainer")
    legacy_cards(meta)
    b.sync(w.state)
    b.sync(w.state)
    mine = [c for c in gh.comments[42] if c["user"]["login"] == gh.login]
    assert len(posts(gh)) == 1 and len(mine) == 1 and other["body"] == quote and not_found(gh, other["id"]) == []
    assert json.loads(meta[f"board:{THREAD}"])["comment_id"] == mine[0]["id"]
