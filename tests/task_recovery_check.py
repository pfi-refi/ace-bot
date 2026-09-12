"""Disposable Postgres and a killed worker process; no production/model/provider calls."""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pgserver

with tempfile.TemporaryDirectory(prefix="ace-recovery-") as tmp:
    server = pgserver.get_server(Path(tmp) / "data", cleanup_mode="delete")
    try:
        os.environ["DATABASE_URL"] = server.get_uri()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        from ace2.backend import db, tasks, taskrunner, capabilities
        db._init_schema(); db._ready = True; tasks.ready()
        def new(title, cap="create_spreadsheet"):
            return tasks.accept(cap, {"title": title}, title=title)[1]["id"]
        def expire(tid):
            with db._conn() as c, c.cursor() as cur:
                cur.execute("UPDATE ace_tasks SET updated_at=now()-interval '1 hour' WHERE id=%s", (tid,))

        tid = new("crashed process")
        code = """
import os,sys
from ace2.backend import tasks
assert tasks.claim(sys.argv[1])
assert tasks.checkpoint(sys.argv[1], {'file_id':'existing-file', 'create_state':'dispatched'})
os._exit(17)
"""
        child = subprocess.run([sys.executable, "-c", code, tid], cwd=ROOT)
        assert child.returncode == 17
        # A new server must not steal still-live work during a rolling deploy.
        assert tasks.recover_interrupted() == []
        expire(tid)
        recovered = tasks.recover_interrupted()
        assert [r["id"] for r in recovered] == [tid]
        assert tasks.get(tid)["state"] == tasks.FAILED
        assert tasks.get(tid)["result"]["file_id"] == "existing-file"
        assert not tasks.claim(tid)
        assert tasks.recover_interrupted() == []

        # A stale attempt cannot overwrite receipts or completion after an explicit retry.
        assert tasks.retry(tid)
        assert tasks.claim(tid)
        token = tasks._attempt.set((tid, 1))
        assert not tasks.checkpoint(tid, {"file_id": "duplicate"})
        tasks.completed(tid, {"file_id": "duplicate"})
        tasks._attempt.reset(token)
        assert tasks.get(tid)["state"] == tasks.WORKING
        assert tasks.get(tid)["result"]["file_id"] == "existing-file"
        assert not tasks.heartbeat(tid, 1)
        assert tasks.heartbeat(tid, 2)
        tasks.failed(tid, "end fixture")

        healthy = new("healthy worker")
        assert tasks.claim(healthy)
        expire(healthy)
        assert tasks.heartbeat(healthy, 1)
        assert tasks.recover_interrupted() == []
        # Expired jobs never re-claim themselves, even before the sweep.
        expire(healthy)
        assert not tasks.claim(healthy)
        tasks.recover_interrupted()
        approval = new("approval")
        tasks.needs_approval(approval, "review1")
        expire(approval)
        tasks.recover_interrupted()
        assert tasks.get(approval)["state"] == tasks.NEEDS_APPROVAL
        resumed = asyncio.run(taskrunner.resume_approved(approval))
        assert resumed["state"] == tasks.FAILED
        assert tasks.get(approval)["attempts"] == 0

        # Recovery drains never-started work once; reconnect/repeated sweeps create no duplicate.
        queued = new("queued after reboot", "fixture")
        calls = []
        async def handler(args, call, progress, result, checkpoint, should_stop):
            calls.append("create")
            await checkpoint({"file_id": "one-artifact"})
            return {"file_id": "one-artifact"}
        async def verify():
            with patch.dict(capabilities.REGISTRY, {"fixture": {"handler": handler}}):
                await taskrunner.recover_once()
                await taskrunner.recover_once()
                await asyncio.gather(*list(taskrunner._bg))
                await taskrunner.recover_once()
            assert calls == ["create"], calls
            assert tasks.get(queued)["state"] == tasks.COMPLETED
        asyncio.run(verify())

        async def delayed_worker():
            delayed = new("delayed old attempt", "delayed_fixture")
            entered, release = asyncio.Event(), asyncio.Event()
            provider_calls = []
            async def paused(args, call, progress, result, checkpoint, should_stop):
                entered.set()
                await release.wait()
                await call("create_artifact", {})
                return {"file_id": "bad-duplicate"}
            async def provider(name, args):
                provider_calls.append(name)
                return "ok"
            with patch.dict(capabilities.REGISTRY, {"delayed_fixture": {"handler": paused}}):
                worker = asyncio.create_task(taskrunner.run(delayed, provider))
                await entered.wait()
                expire(delayed)
                tasks.recover_interrupted()
                assert tasks.retry(delayed)
                assert tasks.claim(delayed)
                release.set()
                await worker
            assert provider_calls == [], provider_calls
            assert tasks.get(delayed)["state"] == tasks.WORKING
            assert tasks.get(delayed)["attempts"] == 2
        asyncio.run(delayed_worker())
        print("PASS: process-death interruption, no automatic replay, preserved receipts, attempt fencing, heartbeat, approval preservation, exactly-once queued recovery")
    finally:
        server.cleanup()
