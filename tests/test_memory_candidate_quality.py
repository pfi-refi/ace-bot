import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import entities, entity_index, entity_migrate


def candidate(name, **overrides):
    base = dict(display=name, sources={'turn:1','turn:2'}, suffix=entity_index.has_org_suffix(name), intros=[], org_ctx=set(), intro_sources=set(), project_sources=set(), name_tokens=True, person_ctx=set(), person_ctx_strong=set())
    return dict(base, **overrides)


def test_numbered_save_filename_is_not_a_person_fact():
    text = '2. **`client_packet_update.md`** — Jordan promised a packet.'
    assert entities.is_meta_narration(text)
    assert entities.classify_source('fact', source_name='sweep', text=text)[1]
    assert not entities.is_meta_narration('Jordan promised a packet.')


def test_possessive_does_not_glue_person_to_place_or_company():
    assert entity_index._strip_possessive("Marlow's Northwind") == 'Marlow'
    assert entity_index._strip_possessive('Marlow Bexley') == 'Marlow Bexley'


def test_generic_company_or_person_company_blend_needs_evidence():
    for name in ('Small Group', 'Family Services', 'Marlow Bexley Northwind Equity'):
        assert entity_migrate.decide_candidate(candidate(name))[0] == ''


def test_explicit_org_context_can_support_a_single_word_name():
    assert entity_migrate.decide_candidate(candidate('Northwind', org_ctx={'turn:1','turn:2'}, org_ctx_strong={'turn:1'}))[0] == 'org'
