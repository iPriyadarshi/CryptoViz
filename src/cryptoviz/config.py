"""
Centralized configuration for CryptoViz.

Every environment variable the application understands is read here and nowhere
else, so the full set of knobs is discoverable in one file and the rest of the
code depends on plain Python values instead of os.getenv() calls scattered
across modules.

Values are resolved at import time from the process environment, after a .env
file (if present) has been loaded.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (three levels up from this file:
# src/cryptoviz/config.py -> src/cryptoviz -> src -> project root).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

PACKAGE_ROOT = Path(__file__).resolve().parent


def _env_bool(name, default=False):
    """Read a boolean environment variable ('true'/'1'/'yes' are truthy)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    """Read an integer environment variable, falling back on bad input."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except TypeError, ValueError:
        return default


def _env_str(name, default):
    """
    Read a string environment variable, treating blank as unset.

    A variable that is present but empty is what a commented-out or
    deliberately blank line in .env produces; it should mean "use the default",
    not "use the empty string".
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


# ---------------------------------------------------------------------------
# Flask / HTTP server
# ---------------------------------------------------------------------------

DEBUG = _env_bool("FLASK_DEBUG", False)
HOST = _env_str("FLASK_HOST", "127.0.0.1")
PORT = _env_int("FLASK_PORT", 5000)

# Origins allowed to call the JSON API. The frontend is same-origin, so this
# only matters for external consumers.
CORS_ORIGINS = _env_str("CORS_ORIGINS", "*")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# Filesystem location of the SQLite database. On a server this should
# point at a durable directory owned by the service user (for example
# /var/lib/cryptoviz/crypto.db); the default keeps development self-contained.
DB_PATH = Path(
    _env_str("CRYPTOVIZ_DB_PATH", str(PROJECT_ROOT / "crypto.db"))
).expanduser()

# How long a connection waits for a competing writer before raising
# "database is locked". The web process and the scraper worker share one SQLite
# file, so this needs to be generous enough to cover a slow write.
DB_BUSY_TIMEOUT_SECONDS = _env_int("CRYPTOVIZ_DB_BUSY_TIMEOUT", 30)

# SQLite read tuning. mmap lets the database be read through the page cache
# without a syscall per page; cache_size is given to SQLite in KiB (passed as a
# negative value, which is how SQLite distinguishes KiB from page counts).
# Both are bounded so a small instance is not starved by them.
DB_MMAP_BYTES = _env_int("CRYPTOVIZ_DB_MMAP_BYTES", 256 * 1024 * 1024)
DB_CACHE_KIB = _env_int("CRYPTOVIZ_DB_CACHE_KIB", 64 * 1024)

# ---------------------------------------------------------------------------
# Background data collection
# ---------------------------------------------------------------------------

# Seconds between successive runs of each scraper loop.
PRICE_UPDATE_INTERVAL = _env_int("CRYPTOVIZ_PRICE_INTERVAL", 300)
SENTIMENT_UPDATE_INTERVAL = _env_int("CRYPTOVIZ_SENTIMENT_INTERVAL", 300)
TOP_GAINERS_UPDATE_INTERVAL = _env_int("CRYPTOVIZ_TOP_GAINERS_INTERVAL", 300)

# Retention windows for each kind of stored data. A full year is kept so the
# charts can show long-range history; see docs/DEPLOYMENT.md for the storage
# this implies.
PRICE_RETENTION_DAYS = _env_int("CRYPTOVIZ_PRICE_RETENTION_DAYS", 365)
SENTIMENT_RETENTION_DAYS = _env_int("CRYPTOVIZ_SENTIMENT_RETENTION_DAYS", 365)
TOP_GAINERS_RETENTION_DAYS = _env_int("CRYPTOVIZ_TOP_GAINERS_RETENTION_DAYS", 365)

# Time windows (in days) the API and the frontend offer. 365 is the longest
# view; requests outside this set fall back to the endpoint's default.
ALLOWED_WINDOW_DAYS = (1, 7, 14, 30, 90, 365)

# Above these window sizes the analytics layer downsamples before loading, so
# a long request returns a readable series instead of one point per scrape.
HOURLY_BUCKET_THRESHOLD_DAYS = _env_int("CRYPTOVIZ_HOURLY_BUCKET_DAYS", 7)
DAILY_BUCKET_THRESHOLD_DAYS = _env_int("CRYPTOVIZ_DAILY_BUCKET_DAYS", 90)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = _env_str("CRYPTOVIZ_LOG_LEVEL", "INFO").upper()
