"""Shared test fixtures.

The database fixture patches db.DB_PATH rather than setting the DB_PATH
environment variable: DB_PATH is read into a module-level constant at import
time, so an env var set after import would be ignored, and a test would
silently run against server/data/commute.db.
"""

import pytest

from app import db as db_mod


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """An open connection to a throwaway database carrying the real schema."""
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
    db_mod.init_db()
    with db_mod.get_conn() as c:
        yield c
