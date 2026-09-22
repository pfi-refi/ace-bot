"""Recover missed indexing notifications without work on a conversation's critical path."""
import asyncio
import logging
import time

logger = logging.getLogger('ace2.entity_maintenance')
INTERVAL_SECONDS = 900
INITIAL_DELAY_SECONDS = 30
STATE = {'runs': 0, 'last_finished': None, 'last_error': None}


def cycle():
    from . import entity_index
    if not entity_index.entities.enabled() or not entity_index._layer_ready():
        return {'ready': False}
    # One awaited pass per interval. Never enqueue overlapping recovery jobs.
    incremental = entity_index.refresh_all(limit_per_corpus=500)
    reconciliation = entity_index.reconcile()
    return {'ready': True, 'incremental': incremental, 'reconciliation': reconciliation}


async def run():
    await asyncio.sleep(INITIAL_DELAY_SECONDS)
    while True:
        try:
            result = await asyncio.to_thread(cycle)
            STATE['runs'] += 1
            STATE['last_finished'] = time.time()
            STATE['last_error'] = ((result.get('reconciliation') or {}).get('errors') or None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            STATE['last_error'] = type(exc).__name__
            logger.warning('memory index recovery failed: %s', type(exc).__name__)
        await asyncio.sleep(INTERVAL_SECONDS)
