import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ace2"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-no-provider")
import asyncio
from unittest.mock import patch
from backend import entity_maintenance as maintenance, entity_index, main


def test_cycle_replays_and_reconciles_when_ready():
    with patch.object(entity_index.entities, 'enabled', return_value=True), \
         patch.object(entity_index, '_layer_ready', return_value=True), \
         patch.object(entity_index, 'refresh_all', return_value={'scanned': 1}) as refresh, \
         patch.object(entity_index, 'reconcile', return_value={'errors': []}) as reconcile:
        assert maintenance.cycle()['ready'] is True
        refresh.assert_called_once_with(limit_per_corpus=500)
        reconcile.assert_called_once_with()


def test_absent_layer_does_not_run_backfill():
    with patch.object(entity_index.entities, 'enabled', return_value=True), \
         patch.object(entity_index, '_layer_ready', return_value=False), \
         patch.object(entity_index, 'refresh_all') as refresh:
        assert maintenance.cycle() == {'ready': False}
        refresh.assert_not_called()


def test_startup_schedules_once_without_waiting_and_shutdown_cancels():
    async def scenario():
        arrived = asyncio.Event()
        async def background():
            arrived.set()
            await asyncio.Event().wait()
        assert main.start_entity_maintenance in main.app.router.on_startup
        assert main.stop_entity_maintenance in main.app.router.on_shutdown
        with patch.object(main, '_entity_maintenance_task', None), patch.object(maintenance, 'run', background):
            await main.start_entity_maintenance()
            task = main._entity_maintenance_task
            await main.start_entity_maintenance()
            assert main._entity_maintenance_task is task
            await asyncio.wait_for(arrived.wait(), 1)
            await main.stop_entity_maintenance()
            assert task.cancelled()
            assert main._entity_maintenance_task is None
    asyncio.run(scenario())
