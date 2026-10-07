"""Bounded #41 follow-ups; these regressions also collect on main 56e9390."""
from dataclasses import replace
from pathlib import Path

import pytest

from fridica_research import machine, replay
from record_corpora import ROOT
from support import CFG, World, report, result


def test_unrelated_value_error_is_not_retried_as_a_brief_failure(monkeypatch):
    w = World()
    w.start()
    snapshot = w.state.to_dict()
    def fail(self):
        raise ValueError('unrelated state invariant')
    monkeypatch.setattr(machine.M, 'run', fail)
    with pytest.raises(ValueError, match='unrelated state invariant'):
        machine.step(w.state, machine.Event('finding', w.now, {'text': 'probe'}), w.cfg)
    assert w.state.to_dict() == snapshot


def test_unrelated_stage_entry_value_error_is_not_retried(monkeypatch):
    w = World()
    w.to_debate()
    def fail(self):
        raise ValueError('invalid stage contract')
    monkeypatch.setattr(machine.M, 'enter_design_audit', fail)
    with pytest.raises(ValueError, match='invalid stage contract'):
        w.finish('mathematician', result(report=report(position='agree')))
        w.finish('physicist', result(report=report(position='agree')))


def test_auditor_design_rule_names_current_preserved_rules():
    text = (Path(__file__).parents[1] / 'src/fridica_research/roles/auditor.md').read_text()
    rule = text.split('34. ', 1)[1]
    assert 'Run rules 29–33' in rule
    assert 'Run rules 15-19' not in rule


def test_protocol_restores_fold_purity_and_effect_event_contract():
    text = (Path(__file__).parents[1] / 'docs/protocol.md').read_text()
    assert '`machine.step(state, event, config) -> (state, actions)` is pure' in text
    assert 'Every outcome of\nexecuting an action comes back as an event' in text
    assert 'the snapshot in the store is always the fold of `step` over the events' in text


def test_final_round_answer_enters_bounded_synthesis_without_extra_round():
    w = World(replace(CFG, max_debate_rounds=1))
    w.to_debate()
    w.finish('mathematician', result(report=report(position='revised', body='## Evidence request\nquestion: measure scaling\nsource: repo\nexperiment: probe')))
    w.finish('physicist', result(report=report(position='agree')))
    assert w.state.phase == 'evidence'
    # Retained historical evidence must not cross an iteration boundary.
    w.state.evidence.append({'iteration': 0, 'design_returns': 0, 'round': 1, 'answer': 'OLD_EVIDENCE_SENTINEL', 'lens': 'mathematician', 'ref': 'old'})
    before = len(w.actions)
    w.finish('explorer', result(report='FINAL_ROUND_SENTINEL'))
    new = w.actions[before:]
    calls = [a for a in new if a.kind == 'llm_call' and a.get('name') == 'study_synthesis']
    assert len(calls) == 1
    assert 'FINAL_ROUND_SENTINEL' in calls[0]['prompt']
    assert 'OLD_EVIDENCE_SENTINEL' not in calls[0]['prompt']
    assert len(calls[0]['prompt']) <= 40_000
    assert not any(a.kind == 'delegate' and a.get('role') == 'debater' for a in new)
    assert w.state.round == 1 and w.state.stage == 'DesignAudit'


def test_evidence_corpus_consumes_answer_and_detects_input_drift(tmp_path):
    source = ROOT / '012_final_round_evidence'
    assert source.is_dir(), 'No replay corpus covers the evidence request/answer path'
    baseline = replay.replay_one(source)
    assert baseline.cls == 'D0', baseline.first
    import json
    import shutil
    target = tmp_path / source.name
    shutil.copytree(source, target)
    events = [json.loads(line) for line in (target / 'events.jsonl').read_text().splitlines()]
    answers = [e for e in events if e['kind'] == 'job_result' and 'FINAL_ROUND_EVIDENCE_41' in str(e['data'].get('result', {}).get('report', ''))]
    assert len(answers) == 1
    answers[0]['data']['result']['report'] = 'ALTERED_EVIDENCE_41'
    (target / 'events.jsonl').write_text(''.join(json.dumps(e, sort_keys=True) + '\n' for e in events))
    changed = replay.replay_one(target)
    assert changed.cls != 'D0'
    actions = (source / 'expected_actions.jsonl').read_text()
    synthesis = [json.loads(line) for line in actions.splitlines() if 'study_synthesis' in line]
    assert any('FINAL_ROUND_EVIDENCE_41' in a.get('prompt', '') for a in synthesis)
