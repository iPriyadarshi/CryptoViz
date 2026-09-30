"""
Shared test fixtures.

Every test runs against a throwaway SQLite file. The database path is read at
import time by cryptoviz.config, so it has to be set in the environment *before*
the package is imported - hence the module-level assignment below rather than a
fixture.
"""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Point the application at a temporary database before importing anything that
# reads the configuration.
_TMP_DB = Path(tempfile.mkdtemp(prefix="cryptoviz-tests-")) / "test.db"
os.environ["CRYPTOVIZ_DB_PATH"] = str(_TMP_DB)
os.environ["CRYPTOVIZ_LOG_LEVEL"] = "WARNING"

from cryptoviz.app import create_app  # noqa: E402
from cryptoviz.db import init_db  # noqa: E402
from cryptoviz.db import repository as repo  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    """Create the schema once for the whole test session."""
    init_db()


@pytest.fixture
def clean_db():
    """Empty every table so a test starts from a known state."""
    far_future = datetime.now() + timedelta(days=3650)
    repo.delete_prices_before(far_future)
    repo.delete_sentiment_before(far_future)
    repo.delete_top_gainers_before(far_future)
    return repo


@pytest.fixture
def price_rows():
    """
    Build three days of hourly prices for two symbols.

    btc trends up and eth trends down, so correlation and drawdown have
    something non-degenerate to work with.
    """
    start = datetime.now() - timedelta(days=3)
    rows = []
    for hour in range(72):
        timestamp = start + timedelta(hours=hour)
        rows.append(
            {
                "symbol": "btc",
                "name": "Bitcoin",
                "price": 60000 + hour * 125.0,
                "market_cap": 1.2e12,
                "volume_24h": 3.0e10,
                "percent_change_24h": 1.5,
                "timestamp": timestamp,
            }
        )
        rows.append(
            {
                "symbol": "eth",
                "name": "Ethereum",
                "price": 3000 - hour * 4.0,
                "market_cap": 4.0e11,
                "volume_24h": 1.0e10,
                "percent_change_24h": -0.8,
                "timestamp": timestamp,
            }
        )
    return rows


@pytest.fixture
def app():
    """A Flask application wired to the temporary database."""
    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    """A test client for the application."""
    return app.test_client()
