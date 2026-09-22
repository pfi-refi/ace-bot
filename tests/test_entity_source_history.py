"""A profile edit must preserve dated, separately addressable source versions."""
from pathlib import Path
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import entity_index


def test_profile_versions_are_not_collapsed_into_one_mutable_source():
    pgserver = pytest.importorskip('pgserver')
    psycopg2 = pytest.importorskip('psycopg2')
    with tempfile.TemporaryDirectory(prefix='ace-profile-source-check-') as folder:
        server = pgserver.get_server(Path(folder) / 'data', cleanup_mode='delete')
        try:
            with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
                cur.execute('CREATE TABLE summaries (id SERIAL PRIMARY KEY, kind TEXT, '
                            'text TEXT, ts TIMESTAMPTZ DEFAULT now())')
                cur.execute("INSERT INTO summaries(kind,text) VALUES "
                            "('ace_profile','Old goal: launch the fictional workshop'), "
                            "('ace_profile','New priority: finish the fictional course')")
                records = entity_index.read_corpus('profile', limit=50, cur=cur)
                assert len(records) == 2, 'profile history disappeared from source inventory'
                assert len({r['native_id'] for r in records}) == 2, 'profile versions share an identity'
                assert {r['native_id'] for r in records} == {'1', '2'}
        finally:
            server.cleanup()
