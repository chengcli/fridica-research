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
    """Cards written before #60 have `commented` and no id: the comment is found by its marker, its id stored, then PATCHed."""
    gh = tb.FakeGh()
    b, gh, meta, w = adopted_world(gh)
    gh.add_comment("chengcli/fridica-research", 42, "An unrelated remark.", "maintainer")
    old = gh.add_comment("chengcli/fridica-research", 42, f"Adopted as the study card of fridica-research study {THREAD} (generation 1); stage cards link here as `Study: #42`. The driver never closes this issue; the PR's `Closes #42` does.", gh.login)
    gh.add_comment("chengcli/fridica-research", 42, "Another remark, pushing the marker past the first page.", "maintainer")
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
