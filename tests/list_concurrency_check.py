"""Disposable Postgres acceptance for concurrent list edits and pool saturation."""
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
import pgserver
from backend import db

with tempfile.TemporaryDirectory(prefix='ace-list-concurrency-') as temp:
    server = pgserver.get_server(Path(temp) / 'data', cleanup_mode='delete')
    try:
        with patch.dict(os.environ, {'DATABASE_URL': server.get_uri()}):
            db._init_schema(); db._ready = True
            assert db.add_custom_list('Build')[0]
            barrier = threading.Barrier(3)
            def edit(fn, *args):
                barrier.wait()
                return fn(*args)
            with ThreadPoolExecutor(max_workers=3) as workers:
                futures = [workers.submit(edit, db.rename_area, 'Build', 'Building'),
                           workers.submit(edit, db.add_custom_list, 'Clients'),
                           workers.submit(edit, db.add_custom_list, 'Operations')]
                assert all(f.result()[0] for f in futures)
            assert set(db.custom_lists()) == {'Building', 'Clients', 'Operations'}
            barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(edit, db.rename_area, 'Groundworks', 'Concrete'),
                           workers.submit(edit, db.rename_area, 'Personal', 'Home')]
                assert all(f.result()[0] for f in futures)
            assert db.area_renames()['Groundworks'] == 'Concrete'
            assert db.area_renames()['Personal'] == 'Home'
            # Duplicate names racing must produce one list, not two successes.
            barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(edit, db.add_custom_list, n) for n in ['Race', 'race']]
                assert sum(f.result()[0] for f in futures) == 1
            # Hold every pool connection; an eleventh caller waits until one is returned.
            holders = [db._conn() for _ in range(10)]
            for holder in holders: holder.__enter__()
            entered = threading.Event()
            def acquire():
                with db._conn(): entered.set()
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(acquire)
                time.sleep(.1)
                assert not future.done(), 'saturation failed immediately instead of waiting'
                holders.pop().__exit__(None, None, None)
                future.result(timeout=3)
                assert entered.is_set()
            for holder in holders: holder.__exit__(None, None, None)
            # Bounded timeout frees no extra slot; pool works after pressure subsides.
            holders = [db._conn() for _ in range(10)]
            for holder in holders: holder.__enter__()
            try:
                with patch.object(db, '_POOL_WAIT_SECONDS', .02):
                    try:
                        with db._conn(): raise AssertionError('exceeded capacity')
                    except TimeoutError: pass
            finally:
                for holder in holders: holder.__exit__(None, None, None)
            with db._conn() as conn, conn.cursor() as cur:
                cur.execute('SELECT 1'); assert cur.fetchone()[0] == 1
            print('PASS: concurrent add/rename, concurrent built-in renames, duplicate race, pool queue and bounded timeout')
    finally:
        server.cleanup()
