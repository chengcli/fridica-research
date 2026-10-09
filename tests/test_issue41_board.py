"""#41 item 2: field-setup refusal must not stop writes to available board fields."""
import copy
import json

import pytest

from fridica_research import board
from support import World, report, result
from test_board import BCFG, FakeGh, make


class DeniedStageMigration(FakeGh):
    def __init__(self):
        super().__init__()
        self.denied = True
        self.options = [o for o in board.STAGE_OPTIONS if o != 'DesignAudit']

    def answer(self, query, variables):
        if query == board.M_UPDATE_FIELD:
            if self.denied:
                raise RuntimeError('FORBIDDEN: UpdateProjectV2Field permission missing')
            self.options = [o['name'] for o in variables['options']]
            return {'updateProjectV2Field': {'projectV2Field': {'id': 'F_stage'}}}
        answer = copy.deepcopy(super().answer(query, variables))
        if query == board.Q_DISCOVER['user']:
            for field in answer['user']['projectV2']['fields']['nodes']:
                if field['name'] == 'Stage':
                    field['options'] = [{'id': 'O_' + o, 'name': o} for o in self.options]
        return answer


@pytest.mark.parametrize('stage', ['Explore', 'DesignAudit'])
def test_field_setup_refusal_keeps_board_sync_and_one_finding(stage, caplog):
    w = World(cfg=BCFG)
    if stage == 'Explore':
        w.start()
    else:
        w.to_debate()
        w.finish('mathematician', result(report=report(position='agree')))
        w.finish('physicist', result(report=report(position='agree')))
        assert w.state.stage == 'DesignAudit'
    snapshot = w.state.to_dict()
    gh = DeniedStageMigration()
    b, _, meta = make(gh)
    first_findings = b.sync(w.state)
    assert gh.argv('gh', 'issue', 'create'), 'field setup stopped all study and stage writes'
    assert gh.sets(board.M_SET_NUMBER), 'available numeric fields were not synced'
    assert gh.sets(board.M_SET_TEXT), 'available text fields were not synced'
    cards = json.loads(meta['board:' + w.state.thread])
    assert len(first_findings) == 1
    assert cards['field_setup_finding'] == first_findings[0]
    assert 'field setup' in first_findings[0].lower()
    bodies = [a[a.index('--body') + 1] for a in gh.argv('gh', 'issue', 'edit') if '--body' in a]
    assert first_findings[0] in bodies[0]
    assert not any(v['v'] == 'O_DesignAudit' for v in gh.sets(board.M_SET_OPTION))
    if stage == 'DesignAudit':
        pending = cards['stages'][-1]['cards'][0]
        assert not pending['filled'], 'an unrepresentable stage must stay pending'
    count = gh.issues
    assert b.sync(w.state) == []
    assert gh.issues == count
    assert caplog.text.count('board field setup failed') == 1
    assert w.state.to_dict() == snapshot  # the board does not mutate fold state
    # Restart uses durable metadata, not an in-memory deduplication flag.
    restarted = board.Board(BCFG, runner=gh, remember=b.remember, recall=b.recall)
    assert restarted.sync(w.state) == []
    assert gh.issues == count
    gh.denied = False
    assert restarted.sync(w.state) == []
    assert gh.issues == count
    assert 'DesignAudit' in restarted.api.project().fields['Stage']['options']
    if stage == 'DesignAudit':
        cards = json.loads(meta['board:' + w.state.thread])
        assert cards['stages'][-1]['cards'][0]['filled']
        assert any(v['v'] == 'O_DesignAudit' for v in gh.sets(board.M_SET_OPTION))
