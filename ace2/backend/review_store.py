"""Durable review records. Approval is a UI action, never a model boolean."""
import json
import uuid
from . import db


def ready():
    if not db.enabled():
        raise RuntimeError('Review storage requires Postgres; nothing executed.')
    with db._conn() as c, c.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS ace_review (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload JSONB NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', result TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '15 minutes')''')


def propose(name, args):
    ready()
    payload = json.dumps({'tool': name, 'args': args}, sort_keys=True)
    with db._conn() as c, c.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(716293)')
        cur.execute("SELECT id FROM ace_review WHERE kind='approval' AND state='pending' AND expires_at>now() AND payload=%s::jsonb ORDER BY created_at DESC LIMIT 1", (payload,))
        row = cur.fetchone()
        if row:
            return row[0]
        ident = uuid.uuid4().hex
        cur.execute("INSERT INTO ace_review(id,kind,payload) VALUES(%s,'approval',%s::jsonb)", (ident, payload))
        return ident


def list_approvals():
    ready()
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT id,payload,CASE WHEN state='pending' AND expires_at<=now() THEN 'expired' ELSE state END,result,created_at,expires_at FROM ace_review WHERE kind='approval' ORDER BY created_at DESC LIMIT 30")
        return [dict(id=r[0], **r[1], state=r[2], result=r[3], created_at=r[4].isoformat(), expires_at=r[5].isoformat()) for r in cur.fetchall()]


def claim(ident, approve):
    ready()
    with db._conn() as c, c.cursor() as cur:
        cur.execute("UPDATE ace_review SET state=%s WHERE id=%s AND kind='approval' AND state='pending' AND expires_at>now() RETURNING payload", ('executing' if approve else 'rejected', ident))
        row = cur.fetchone()
        if not row:
            raise ValueError('This proposal expired or was already handled. Refresh the tray.')
        return row[0]


def finish(ident, state, result):
    if state not in ('succeeded', 'failed', 'unknown'):
        raise ValueError('Invalid result state')
    with db._conn() as c, c.cursor() as cur:
        cur.execute("UPDATE ace_review SET state=%s,result=%s WHERE id=%s AND state='executing'", (state, str(result), ident))


def append_plan(role, text):
    ready()
    with db._conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO ace_review(id,kind,payload,state) VALUES(%s,'plan',%s::jsonb,'draft')", (uuid.uuid4().hex, json.dumps({'role': role, 'text': text})))


def read_plan():
    ready()
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT payload,created_at FROM ace_review WHERE kind='plan' AND created_at>now()-interval '7 days' ORDER BY created_at DESC LIMIT 60")
        return [dict(**r[0], saved_at=r[1].isoformat()) for r in reversed(cur.fetchall())]
