"""An unavailable database must not masquerade as an empty memory."""
from contextlib import contextmanager
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import db, main


@contextmanager
def unavailable_connection():
    raise ConnectionError('synthetic database outage')
    yield  # preserve the context-manager interface


@pytest.mark.parametrize('path', [
    '/entities', '/entities/counts', '/entities/review',
    '/sources/search?q=Jordan', '/graph?source=entities',
])
def test_unavailable_index_is_not_reported_as_zero_records(monkeypatch, path):
    monkeypatch.setattr(db, 'enabled', lambda: True)
    monkeypatch.setattr(db, '_conn', unavailable_connection)
    old = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[main.require_auth] = lambda: None
    try:
        # No context-manager startup: background tasks must not run in this probe.
        response = TestClient(main.app).get(path)
        assert response.status_code == 503, response.json()
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(old)
