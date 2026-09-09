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

    # SAME DAY, MIXED TIER (Codex, 8 Sept). Truncating to a day string made these two
    # indistinguishable, and the stable sort then kept the read path's importance order —
    # the evening correction printing before the morning note it superseded.
    MORNING = 'Renner pour is booked for Thursday'
    EVENING = 'Renner correction: the pour moved to Friday, not Thursday'
    with db._conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO facts(text, tier, ts) VALUES(%s,'active','2026-09-08 09:00')",
                    (MORNING,))
        cur.execute("INSERT INTO facts(text, tier, ts) VALUES(%s,'core','2026-09-08 17:00')",
                    (EVENING,))
    raw2 = brain.read_memory()
    assert raw2.index(EVENING) < raw2.index(MORNING), 'read path should be importance-first'
    meta2 = brain.read_memory_meta()
    out2 = chat._group_facts(raw2, min_facts=2, meta=meta2)
    block2 = out2.split('RENNER')[1].split(chr(10) + chr(10))[0]
    rows = [l for l in block2.splitlines() if l.startswith('  - ')]
    assert 'Thursday' in rows[0] and '09:00' in rows[0], 'morning note must lead: ' + rows[0]
    assert 'Friday' in rows[-1] and '17:00' in rows[-1], 'evening must land last: ' + rows[-1]
    assert 'recorded' in rows[0], rows[0]

    # facts.ts is NOT NULL in Postgres, so an undated fact can only arrive from the Drive
    # fallback or from meta that does not cover it. That is where the [undated] path lives.
    partial = dict(meta2); partial.pop(MORNING, None)
    out3 = chat._group_facts(raw2, min_facts=2, meta=partial)
    assert '[undated]' in out3, 'a fact with no known recording time must say so'
    rows3 = [l for l in out3.split('RENNER')[1].split(chr(10) + chr(10))[0].splitlines()
             if l.startswith('  - ')]
    assert '[undated]' in rows3[0], 'an unknown time sorts first and settles nothing'

    print('PASS: the real read path returns core-tier first, the formatter still presents '
          'the newest DATE last, every line is dated, the stored order is untouched, same-day mixed tiers order by minute, and unknown dates stay unknown.')
finally:
    server.cleanup(); tmp.cleanup()
