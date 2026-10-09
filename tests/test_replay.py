"""R19: every checked-in corpus replays identically (D0); text churn is D1, a changed action is D2, an added field is D3; the CLI exit codes."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from fridica_research import briefs, cli, machine, replay
from record_corpora import ROOT, SCENARIOS, record

CORPORA = sorted(p.name for p in ROOT.iterdir() if replay.is_corpus(p))


def test_every_scenario_is_checked_in():
    assert CORPORA == sorted(SCENARIOS)


@pytest.mark.parametrize("name", CORPORA)
def test_corpus_replays_identically(name):
    r = replay.replay_one(ROOT / name)
    assert r.cls == "D0", r.first


@pytest.mark.parametrize("name", CORPORA)
def test_corpus_is_up_to_date_with_its_scenario(name, tmp_path):
    """The checked-in corpus equals a fresh recording: the scenario, not the files, is the source."""
    fresh = record(name, tmp_path)
    for f in replay.FILES: assert (fresh / f).read_text() == (ROOT / name / f).read_text(), f


def test_report_lists_every_corpus_with_its_class():
    r = replay.replay(ROOT)
    assert r.ok and [c.name for c in r.corpora] == CORPORA and {c.cls for c in r.corpora} == {"D0"}
    assert r.text().endswith(f"{len(CORPORA)}/{len(CORPORA)} pass")


def test_brief_text_perturbation_is_textual(monkeypatch):
    orig = briefs.explorer
    monkeypatch.setattr(briefs, "explorer", lambda *a, **k: orig(*a, **k) + "\nnote: one more sentence in the template")
    r = replay.replay(ROOT / "001_simple_research")
    assert r.corpora[0].cls == "D1" and "brief" in r.corpora[0].first and r.ok
    assert not replay.replay(ROOT / "001_simple_research", strict=True).ok


def test_changed_action_kind_is_structural(monkeypatch):
    monkeypatch.setattr(machine.M, "board", lambda self: self.emit("board_sync", self.aid("board"), thread=self.s.thread))
    r = replay.replay(ROOT / "001_simple_research", strict=False, accept_added_fields=True)
    assert r.corpora[0].cls == "D2" and "board_update" in r.corpora[0].first and not r.ok


def test_contract_line_change_is_structural(monkeypatch):
    orig = briefs.explorer
    monkeypatch.setattr(briefs, "explorer", lambda ref, *a, **k: orig(ref + "x", *a, **k))
    r = replay.replay(ROOT / "001_simple_research")
    assert r.corpora[0].cls == "D2" and "contract lines differ" in r.corpora[0].first


def test_added_action_field_is_schema(monkeypatch):
    monkeypatch.setattr(machine.M, "board", lambda self: self.emit("board_update", self.aid("board"), thread=self.s.thread, future_generation=self.s.generation))
    r = replay.replay(ROOT / "001_simple_research")
    assert r.corpora[0].cls == "D3" and r.corpora[0].added and not r.ok
    assert replay.replay(ROOT / "001_simple_research", accept_added_fields=True).ok


def test_added_state_field_is_schema_and_missing_is_structural(tmp_path):
    src = ROOT / "001_simple_research"
    for name, mutate, cls in (("added", lambda d: d.pop("notes"), "D3"), ("missing", lambda d: d.__setitem__("not_a_field", 1), "D2"), ("changed", lambda d: d.__setitem__("iteration", 9), "D2")):
        dst = tmp_path / name
        shutil.copytree(src, dst)
        d = json.loads((dst / "expected_final_state.json").read_text())
        mutate(d)
        (dst / "expected_final_state.json").write_text(json.dumps(d, sort_keys=True))
        r = replay.replay_one(dst)
        assert r.cls == cls and r.first.startswith("state."), (name, r.first)


def test_structural_beats_added_field_beats_textual(monkeypatch):
    orig = briefs.explorer
    monkeypatch.setattr(briefs, "explorer", lambda *a, **k: orig(*a, **k) + "\nnote: churn")
    monkeypatch.setattr(machine.M, "board", lambda self: self.emit("board_update", self.aid("board"), thread=self.s.thread, extra=1))
    assert replay.replay(ROOT / "001_simple_research").corpora[0].cls == "D3"
    monkeypatch.setattr(machine, "ORDER", machine.ORDER)  # no-op; the kind change below is the structural one
    monkeypatch.setattr(machine.M, "board", lambda self: self.emit("board_sync", self.aid("board"), thread=self.s.thread, extra=1))
    assert replay.replay(ROOT / "001_simple_research").corpora[0].cls == "D2"


def test_token_like_text_fails_the_corpus(tmp_path):
    dst = tmp_path / "leaky"
    shutil.copytree(ROOT / "001_simple_research", dst)
    with (dst / "events.jsonl").open("a") as f: f.write(json.dumps({"kind": "finding", "now": 1.0, "data": {"text": "token ghp_abcdef0123"}}) + "\n")
    r = replay.replay_one(dst)
    assert r.cls == "LEAK" and "events.jsonl" in r.first and not replay.replay(dst, accept_added_fields=True).ok


def test_checked_in_corpora_are_redaction_clean():
    assert [replay.leaks(ROOT / n) for n in CORPORA] == [[] for _ in CORPORA]


def test_volatile_null_to_set_is_structural(tmp_path):
    """F1: masking hides the value of a volatile field, not whether it is set."""
    dst = tmp_path / "unset"
    shutil.copytree(ROOT / "001_simple_research", dst)
    d = json.loads((dst / "expected_final_state.json").read_text())
    assert d["finished_at"] is not None
    d["finished_at"] = None
    (dst / "expected_final_state.json").write_text(json.dumps(d, sort_keys=True))
    r = replay.replay_one(dst)
    assert r.cls == "D2" and r.first.startswith("state.finished_at")


def test_text_type_change_is_structural(tmp_path):
    """F2: a text key going from a string to null (or back) is D2, not D1."""
    dst = tmp_path / "typed"
    shutil.copytree(ROOT / "001_simple_research", dst)
    rows = [json.loads(line) for line in (dst / "expected_actions.jsonl").read_text().splitlines()]
    i = next(i for i, a in enumerate(rows) if a["kind"] == "post" and a["details"] is None)
    rows[i]["details"] = "was a string"
    (dst / "expected_actions.jsonl").write_text("".join(json.dumps(a) + "\n" for a in rows))
    r = replay.replay_one(dst)
    assert r.cls == "D2" and f"actions[{i}].details" in r.first and "type changed" in r.first


def test_every_contract_line_is_structural(monkeypatch):
    """F3: the why/also-considered lines of a claim are parsed by peers, so a change there is D2."""
    orig = briefs.guard_post
    monkeypatch.setattr(briefs, "guard_post", lambda text, details: orig(text.replace("why: ", "why: changed "), details))
    r = replay.replay(ROOT / "001_simple_research")
    assert r.corpora[0].cls == "D2" and "contract lines differ" in r.corpora[0].first and "why" in r.corpora[0].first


def test_classes_are_judged_per_field_never_masked(monkeypatch):
    """F4: a D3 accepted with --accept-added-fields does not hide a D1 under --strict; the report names every class."""
    orig = briefs.explorer
    monkeypatch.setattr(briefs, "explorer", lambda *a, **k: orig(*a, **k) + "\nnote: churn")
    monkeypatch.setattr(machine.M, "board", lambda self: self.emit("board_update", self.aid("board"), thread=self.s.thread, extra=1))
    r = replay.replay(ROOT / "001_simple_research", strict=True, accept_added_fields=True)
    assert r.corpora[0].cls == "D3" and r.corpora[0].classes == ["D3", "D1"] and not r.ok and "D3+D1" in r.text()
    assert replay.replay(ROOT / "001_simple_research", strict=False, accept_added_fields=True).ok


def test_volatile_fields_are_masked(tmp_path):
    dst = tmp_path / "shifted"
    shutil.copytree(ROOT / "001_simple_research", dst)
    d = json.loads((dst / "expected_final_state.json").read_text())
    d["started_at"] += 1
    (dst / "expected_final_state.json").write_text(json.dumps(d, sort_keys=True))
    assert replay.replay_one(dst).cls == "D0"


def test_cli_exit_codes(capsys, monkeypatch):
    assert cli.main(["replay", str(ROOT)]) == 0
    assert "D0" in capsys.readouterr().out
    orig = briefs.explorer
    monkeypatch.setattr(briefs, "explorer", lambda *a, **k: orig(*a, **k) + "\nnote: churn")
    assert cli.main(["replay", str(ROOT / "001_simple_research")]) == 0
    assert cli.main(["replay", str(ROOT / "001_simple_research"), "--strict"]) == 1
    monkeypatch.setattr(machine.M, "board", lambda self: self.emit("board_update", self.aid("board"), thread=self.s.thread, extra=1))
    assert cli.main(["replay", str(ROOT / "001_simple_research")]) == 1
    assert cli.main(["replay", str(ROOT / "001_simple_research"), "--accept-added-fields"]) == 0
    assert cli.main(["replay", str(Path(__file__).parent)]) == 1  # no corpus there
