import json

import council_synthesis as synthesis


BUNDLE = {'question': '¿Qué hacemos?', 'heads': ['melchior','balthasar','casper'], 'contributions': []}
SEATS = [{'seat': s, 'type': 'api'} for s in BUNDLE['heads']]
DRAFT = {'answer': 'Probar primero, conservando los datos.', 'agreements': ['Conservar datos'],
         'differences': ['El plazo sigue en discusión'], 'open_questions': []}


def test_three_reviews_required_and_disagreement_preserved():
    calls = []
    def invoke(seat, prompt):
        calls.append(seat['seat'])
        if 'Redactá una respuesta' in prompt:
            return json.dumps(DRAFT)
        return json.dumps({'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'reviewed'
    assert len(result['reviews']) == 3
    assert result['differences'] == DRAFT['differences']
    assert len(calls) == 4


def test_dissent_never_becomes_consensus_and_budget_is_bounded():
    calls = []
    def invoke(seat, prompt):
        calls.append(prompt)
        if 'Redactá una respuesta' in prompt:
            return json.dumps(DRAFT)
        return json.dumps({'approve': seat['seat'] != 'casper', 'feedback': 'Falta el costo'})
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'partial'
    assert result['cycle'] == 2
    assert len(calls) == 8
    assert 'Falta el costo' in calls[4]


def test_missing_reviewer_is_not_approval():
    def invoke(seat, prompt):
        return json.dumps(DRAFT if 'Redactá una respuesta' in prompt else {'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS[:2], invoke)
    assert result['status'] == 'partial'
    assert result['reviews'][-1]['approve'] is False


def test_stale_result_is_not_published():
    class Transaction:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    class Conn:
        def transaction(self): return Transaction()
        def execute(self, sql, params=()):
            assert not sql.startswith('UPDATE')
            self.sql = sql
            return self
        def fetchone(self):
            if 'SELECT *' in self.sql:
                return {'round': 2, 'status': 'open', 'thread': 'd1'}
            return {'id': 99}
    assert not synthesis.save(Conn(), 1, {'round':1,'status':'closed','message_id':42}, DRAFT)


def test_broken_reviewer_output_is_not_approval():
    def invoke(seat, prompt):
        if 'Redactá una respuesta' in prompt:
            return json.dumps(DRAFT)
        return 'Provider error: invalid model'
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'partial'
    assert not any(review['approve'] for review in result['reviews'])
