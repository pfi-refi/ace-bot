"""Recent direct context and source cautions survive the normal prompt budget."""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import entities, entity_context


def test_recent_user_source_is_not_hidden_behind_old_saved_claims():
    dossier = {
        'entity': {'entity_id': 'per_aaaaaaaaaaaa', 'display_name': 'Jordan Rivera',
                   'type': 'person', 'review_status': 'unreviewed'},
        'current': [dict(attribute='plan', value='An older planning statement. ' * 12,
                         source_class='legacy_extracted', source_id=f'fact:{i}',
                         stated_at='2026-08-01T12:00:00+00:00') for i in range(20)],
        'sources': [dict(role='user', source_class='user_statement', source_id='turn:99',
                         occurred_at='2026-09-21T12:00:00+00:00',
                         excerpt='The fictional course is now the priority.')],
        'counts': {'sources_linked': 21, 'sources_shown': 1},
        'items': [{'item_id': 'fixture1', 'status': 'open', 'live': True,
                   'text': 'Prepare the fictional course outline'}],
    }
    with patch.object(entity_context, 'available', return_value=True), \
         patch.object(entities, 'dossier', return_value=dossier):
        rendered = entity_context.dossier_text('per_aaaaaaaaaaaa')
    assert len(rendered) <= entity_context.DOSSIER_CHARS
    assert 'fictional course is now the priority' in rendered
    assert 'turn:99' in rendered
    assert entities.DATA_NOTE in rendered
    assert entities.MONEY_NOTE in rendered
    assert 'PARTIAL RECORD' in rendered
    assert 'BOARD RECORDS' in rendered
    assert 'Prepare the fictional course outline' in rendered
    assert "'today' refer to the statement's source date" in rendered


def test_truncation_cannot_reparent_a_conflict_under_a_previous_heading():
    lines = ['Saved claims:', 'x' * 120, '- a short conflicting claim']
    rendered = entity_context._bounded(lines, 100)
    assert 'short conflicting claim' not in rendered
    disputed = entity_context._fact_line({'attribute':'org','value':'Northwind','conflicts_with':[2]})
    assert 'DISPUTED' in disputed


def test_source_reclipping_preserves_both_data_delimiters():
    wrapped = entities.wrap_excerpt('A long fictional record. ' * 40, 700)
    rendered = entity_context._reclip_excerpt(wrapped, 180)
    assert rendered.startswith('<<<src ')
    assert rendered.endswith('>>>')
