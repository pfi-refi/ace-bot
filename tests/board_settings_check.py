"""Real /daybank query budget, payload equivalence and next-request freshness.

Disposable Postgres and synthetic rows only. No API/model calls or production data.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'ace2')]
import pgserver
from fastapi.testclient import TestClient

tmp = tempfile.TemporaryDirectory(prefix='ace-settings-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    from ace2.backend import db, classify
    from ace2.backend.main import app
    db._init_schema(); db._ready = True; db._trgm_ok = False
    client = TestClient(app)
    db.add_summary('{"Groundworks":"Concrete"}', 'board_list_renames')
    db.add_summary('2026-09-09T00:00:00+00:00', 'board_review_boundary')

    def seed(n):
        with db._conn() as c, c.cursor() as cur:
            cur.execute('DELETE FROM daybank_items')
            for i in range(n):
                cur.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,entry,bucket) "
                            "VALUES (%s,'2026-09-08T00:00:00+00:00','todo',%s,%s,'[]',%s,%s)",
                            (str(i), 'groundworks synthetic item ' + str(i),
                             'done' if i % 7 == 0 else 'open',
                             'action' if i % 2 else 'record',
                             'Side Work' if i % 5 == 0 else None))

    for n in (5, 500):
        seed(n)
        for active in (False, True):
            with patch.object(db, '_conn', wraps=db._conn) as conns, \
                 patch.object(db, 'latest_summary', wraps=db.latest_summary) as summaries:
                response = client.get('/daybank', params={'all': str(not active).lower(), 'suggest': 4})
                assert response.status_code == 200, response.text
                payload = response.json()
                assert conns.call_count == 3, (n, active, conns.call_count)
                assert [c.args[0] for c in summaries.call_args_list] == [
                    'board_list_renames', 'board_review_boundary']
            # The pre-change presentation path independently reads the setting for every
            # unclassified record. Its complete outputs must be byte-equivalent as data.
            rows = db.read_items(active)
            assert payload['items'] == [classify.decorate(r) for r in rows]
            assert payload['summary'] == classify.summarise(rows)
            assert payload['due_today'] == classify.due_today_sections(rows, payload['today'], 4)
            print(f'PASS: {n} rows, active={active}: three DB reads; identical payload')

    # No process cache: a subsequent request sees both kinds of changed settings.
    before = client.get('/daybank?all=true').json()
    db.add_summary('{"Groundworks":"Foundations"}', 'board_list_renames')
    db.add_summary('2026-09-01T00:00:00+00:00', 'board_review_boundary')
    after = client.get('/daybank?all=true').json()
    assert any(r['area'] == 'Concrete' for r in before['items'])
    assert any(r['pre_release_one'] for r in before['items'])
    assert not any(r['area'] == 'Concrete' for r in after['items'])
    assert any(r['area'] == 'Foundations' for r in after['items'])
    assert not any(r['pre_release_one'] for r in after['items'])
    assert {r['id'] for r in before['items']} == {r['id'] for r in after['items']}
    assert all(r['area'] == 'Side Work' for r in after['items'] if r['bucket_set'])
    print('PASS: next request sees changed settings; stored overrides and identities preserved')

    # A concurrent setting update cannot give the response mixed old/new classifications.
    original_read = db.read_items
    def change_after_snapshot(*args, **kwargs):
        db.add_summary('{"Groundworks":"New name"}', 'board_list_renames')
        return original_read(*args, **kwargs)
    with patch.object(db, 'read_items', side_effect=change_after_snapshot):
        consistent = client.get('/daybank?all=true').json()
    assert not any(r['area'] == 'New name' for r in consistent['items'])
    assert 'New name' not in consistent['summary']['areas']
    assert any(r['area'] == 'New name' for r in client.get('/daybank?all=true').json()['items'])
    print('PASS: one response uses one mapping even when it changes during row loading')
finally:
    server.cleanup()
    tmp.cleanup()
