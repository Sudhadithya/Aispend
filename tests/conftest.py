import pytest

from aispend.storage import db


@pytest.fixture(scope="session", autouse=True)
def _close_pool_at_session_end():
    yield
    db.close_pool()
