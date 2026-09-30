"""
Database engine construction and connection tuning.

The deployment target is a single machine where two processes - the web server
and the scraper worker - share one local SQLite file. That shapes the pragmas
applied below: WAL journalling so a reader is never blocked by the writer, and a
busy timeout so the loser of a write race waits instead of immediately raising
"database is locked".
"""

import logging
import sqlite3

from sqlalchemy import create_engine, event

from .. import config
from .models import metadata

logger = logging.getLogger(__name__)

_engine = None


def _build_connection_url():
    """
    Build the SQLAlchemy connection URL for the local SQLite database.

    Returns:
        tuple[str, dict]: (url, connect_args)
    """
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False is required because the web process serves
    # requests from multiple threads, and the worker runs its scraper loops in
    # threads of its own.
    connect_args = {
        "check_same_thread": False,
        "timeout": config.DB_BUSY_TIMEOUT_SECONDS,
    }
    return f"sqlite:///{config.DB_PATH}", connect_args


def _apply_sqlite_pragmas(dbapi_connection, _connection_record):
    """
    Configure each new SQLite connection for safe multi-process access.

    WAL lets the web process keep reading while the worker writes; NORMAL
    synchronous is the standard companion to WAL (durable across application
    crashes, and only at risk from an OS-level crash); busy_timeout makes a
    blocked writer wait rather than fail.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={config.DB_BUSY_TIMEOUT_SECONDS * 1000}")
        cursor.execute("PRAGMA foreign_keys=ON")

        # Read tuning. The workload is read-heavy over a database of a few
        # hundred MB, so letting SQLite map the file and hold more pages in
        # cache saves syscalls on the analytics scans. The effect is small on a
        # warm page cache and larger on a cold one.
        cursor.execute(f"PRAGMA mmap_size={config.DB_MMAP_BYTES}")
        cursor.execute(f"PRAGMA cache_size=-{config.DB_CACHE_KIB}")
    finally:
        cursor.close()


def get_engine():
    """Return the shared SQLAlchemy engine, creating it on first use."""
    global _engine
    if _engine is None:
        url, connect_args = _build_connection_url()
        _engine = create_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,
            future=True,
        )
        event.listen(_engine, "connect", _apply_sqlite_pragmas)
        logger.info("Using SQLite database at %s", config.DB_PATH)
    return _engine


def init_db():
    """Create all tables if they do not already exist."""
    metadata.create_all(get_engine())
