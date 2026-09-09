"""Fact ordering through the REAL read path and a real disposable PostgreSQL.

The unit test started from an already-ordered list and therefore could not see the defect:
read_facts() sorts by (tier='core') DESC, id — importance first, not chronology.
Synthetic facts only; no live data.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'ace2'))
import pgserver                                        # noqa: E402

tmp = tempfile.TemporaryDirectory(prefix='ace-facts-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    from ace2.backend import db, brain, chat            # noqa: E402
    db._init_schema(); db._ready = True; db._trgm_ok = False

    STALE = 'Marlow project is on Thursday'
    FIX = 'Marlow correction: project is on Friday, not Thursday'
    with db._conn() as c, c.cursor() as cur:
        # inserted oldest-first, but the CORRECTION is core tier — which is what makes the
        # read path return it FIRST and the stale line last.
        cur.execute("INSERT INTO facts(text, tier, ts) VALUES(%s,'active','2026-09-01')", (STALE,))
        cur.execute("INSERT INTO facts(text, tier, ts) VALUES(%s,'core','2026-09-08')", (FIX,))
        cur.execute("INSERT INTO facts(text, tier, ts) VALUES(%s,'active','2026-08-01')",
                    ('Marlow first met at the supply yard',))

    raw = brain.read_memory()
    assert raw, 'no facts read back'
    # The defect, demonstrated: the real read path puts the newest correction FIRST.
    assert raw.index(FIX) < raw.index(STALE), (
        'expected the read path to return core-tier first; ordering assumption changed')

    meta = brain.read_memory_meta()
    assert meta.get(FIX, {}).get('ts'), 'timestamps must reach the formatter'

    out = chat._group_facts(raw, min_facts=2, meta=meta)
    lines = [l for l in out.splitlines() if l.startswith('  - ')]
    assert lines, out
    # ...and the formatter must nonetheless present the newest DATE last.
    assert '2026-09-08' in lines[-1], f'stale line presented as newest: {lines[-1]}'
    assert 'Friday' in lines[-1], lines[-1]
    assert '2026-08-01' in lines[0], f'oldest must lead: {lines[0]}'
    # every line carries its date, so recency is never inferred from position
    for l in lines:
        assert l.strip()[2:].startswith('['), f'undated line in context: {l}'
    # nothing was reordered or rewritten in the STORE
    again = brain.read_memory()
    assert again == raw, 'the store was mutated to make the output look right'

    print('PASS: the real read path returns core-tier first, the formatter still presents '
          'the newest DATE last, every line is dated, and the stored order is untouched.')
finally:
    server.cleanup(); tmp.cleanup()
