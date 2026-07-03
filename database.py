"""
Database Layer for CryptoViz

This module centralizes all data persistence for the platform. It replaces the
previous flat-file storage (crypto_data.csv and the data/*.json sentiment / top
gainers files) with a proper database.

By default it connects to a Turso (libSQL / cloud SQLite) database using the
`sqlalchemy-libsql` dialect. When Turso credentials are not configured, it
transparently falls back to a local SQLite file (crypto.db) so the app remains
fully runnable during development.

Configuration (via environment variables / .env):
    TURSO_DATABASE_URL   e.g. libsql://your-db-name-org.turso.io
    TURSO_AUTH_TOKEN     the auth token generated for that database

Schema:
    crypto_prices             normalized time-series price rows (queried by the
                              analytics layer for history, volatility, correlation)
    sentiment_snapshots       one row per sentiment scrape; the full sentiment
                              JSON blob is stored verbatim + a created_at timestamp
    top_gainers_snapshots     one row per top-gainers scrape; the JSON list is
                              stored verbatim + a created_at timestamp
"""

import os
import json
from datetime import datetime

import pandas as pd
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    Integer,
    String,
    Float,
    Text,
    Index,
    insert,
    select,
    delete,
)

# Timestamp format used consistently throughout the application. Storing
# timestamps as text in this exact format keeps the analytics code (which parses
# with this format) working without changes.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# Engine / schema definition
# ---------------------------------------------------------------------------

metadata = MetaData()

crypto_prices = Table(
    "crypto_prices",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(32), nullable=False),
    Column("name", String(128)),
    Column("price", Float),
    Column("market_cap", Float),
    Column("volume_24h", Float),
    Column("percent_change_24h", Float),
    # Stored as text in TIMESTAMP_FORMAT for consistent parsing.
    Column("timestamp", String(32), nullable=False),
    Index("ix_crypto_prices_symbol_ts", "symbol", "timestamp"),
)

sentiment_snapshots = Table(
    "sentiment_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", String(32), nullable=False),
    Column("data", Text, nullable=False),  # full sentiment JSON blob
    Index("ix_sentiment_created_at", "created_at"),
)

top_gainers_snapshots = Table(
    "top_gainers_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", String(32), nullable=False),
    Column("data", Text, nullable=False),  # JSON list of top gainer dicts
    Index("ix_top_gainers_created_at", "created_at"),
)


_engine = None


def _build_connection_url():
    """
    Build the SQLAlchemy connection URL.

    Returns a tuple of (url, connect_args). Uses Turso/libSQL when configured,
    otherwise a local SQLite file for development.
    """
    turso_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    turso_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()

    if turso_url:
        # Normalize the host: strip any scheme prefix Turso may include.
        host = (
            turso_url.replace("libsql://", "")
            .replace("https://", "")
            .replace("wss://", "")
            .rstrip("/")
        )
        # sqlalchemy-libsql dialect: sqlite+libsql://<host>/?authToken=...&secure=true
        url = f"sqlite+libsql://{host}/?authToken={turso_token}&secure=true"
        return url, {}

    # Local development fallback: single-file SQLite. check_same_thread=False is
    # required because the Flask app writes from background scraper threads and
    # reads from request threads.
    return "sqlite:///crypto.db", {"check_same_thread": False}


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
    return _engine


def init_db():
    """Create all tables if they do not already exist."""
    metadata.create_all(get_engine())


# ---------------------------------------------------------------------------
# Price data (replaces crypto_data.csv)
# ---------------------------------------------------------------------------

_PRICE_COLUMNS = [
    "symbol",
    "name",
    "price",
    "market_cap",
    "volume_24h",
    "percent_change_24h",
    "timestamp",
]


def _normalize_timestamp(value):
    """Coerce a timestamp value into the canonical string format."""
    if isinstance(value, str):
        # Reformat to be safe/consistent; fall back to the raw string if parsing fails.
        try:
            return pd.to_datetime(value).strftime(TIMESTAMP_FORMAT)
        except Exception:
            return value
    if isinstance(value, datetime):
        return value.strftime(TIMESTAMP_FORMAT)
    try:
        return pd.to_datetime(value).strftime(TIMESTAMP_FORMAT)
    except Exception:
        return datetime.now().strftime(TIMESTAMP_FORMAT)


def save_prices(data):
    """
    Persist a batch of cryptocurrency price records.

    Args:
        data (list[dict]): rows containing the _PRICE_COLUMNS keys.
    """
    if not data:
        return

    rows = []
    for entry in data:
        rows.append(
            {
                "symbol": entry.get("symbol"),
                "name": entry.get("name"),
                "price": entry.get("price"),
                "market_cap": entry.get("market_cap"),
                "volume_24h": entry.get("volume_24h"),
                "percent_change_24h": entry.get("percent_change_24h"),
                "timestamp": _normalize_timestamp(entry.get("timestamp")),
            }
        )

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(insert(crypto_prices), rows)


def get_prices_df(symbol=None):
    """
    Load price rows into a DataFrame with a parsed datetime 'timestamp' column.

    Args:
        symbol (str, optional): restrict to a single symbol.

    Returns:
        pandas.DataFrame with columns: symbol, name, price, market_cap,
        volume_24h, percent_change_24h, timestamp (datetime64). Empty DataFrame
        (with the right columns) if there is no data.
    """
    engine = get_engine()
    stmt = select(
        crypto_prices.c.symbol,
        crypto_prices.c.name,
        crypto_prices.c.price,
        crypto_prices.c.market_cap,
        crypto_prices.c.volume_24h,
        crypto_prices.c.percent_change_24h,
        crypto_prices.c.timestamp,
    )
    if symbol is not None:
        stmt = stmt.where(crypto_prices.c.symbol == symbol)

    df = pd.read_sql(stmt, engine)

    if df.empty:
        return pd.DataFrame(columns=_PRICE_COLUMNS)

    df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed")
    return df


def delete_prices_before(cutoff):
    """
    Remove price rows older than the given cutoff datetime.

    Returns the number of rows deleted.
    """
    cutoff_str = cutoff.strftime(TIMESTAMP_FORMAT)
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            delete(crypto_prices).where(crypto_prices.c.timestamp < cutoff_str)
        )
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# Sentiment data (replaces data/sentiment_*.json + latest_sentiment_data.json)
# ---------------------------------------------------------------------------


def save_sentiment_snapshot(sentiment_data):
    """Insert a new sentiment snapshot (full JSON blob) with a created_at time."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(sentiment_snapshots),
            {
                "created_at": datetime.now().strftime(TIMESTAMP_FORMAT),
                "data": json.dumps(sentiment_data),
            },
        )


def get_latest_sentiment():
    """Return the most recent sentiment data dict, or None if none exists."""
    engine = get_engine()
    stmt = (
        select(sentiment_snapshots.c.data)
        .order_by(sentiment_snapshots.c.created_at.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    if not row:
        return None
    return json.loads(row[0])


def get_latest_sentiment_time():
    """Return the datetime of the most recent sentiment snapshot, or None."""
    engine = get_engine()
    stmt = (
        select(sentiment_snapshots.c.created_at)
        .order_by(sentiment_snapshots.c.created_at.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    if not row:
        return None
    return datetime.strptime(row[0], TIMESTAMP_FORMAT)


def get_sentiment_snapshots_since(cutoff):
    """
    Return sentiment snapshots created on/after cutoff, oldest first.

    Returns:
        list[tuple[datetime, dict]]: (created_at, sentiment_data)
    """
    cutoff_str = cutoff.strftime(TIMESTAMP_FORMAT)
    engine = get_engine()
    stmt = (
        select(sentiment_snapshots.c.created_at, sentiment_snapshots.c.data)
        .where(sentiment_snapshots.c.created_at >= cutoff_str)
        .order_by(sentiment_snapshots.c.created_at.asc())
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()

    result = []
    for created_at, data in rows:
        try:
            result.append((datetime.strptime(created_at, TIMESTAMP_FORMAT), json.loads(data)))
        except Exception:
            continue
    return result


def delete_sentiment_before(cutoff):
    """Remove sentiment snapshots older than cutoff. Returns rows deleted."""
    cutoff_str = cutoff.strftime(TIMESTAMP_FORMAT)
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            delete(sentiment_snapshots).where(
                sentiment_snapshots.c.created_at < cutoff_str
            )
        )
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# Top gainers data (replaces data/top_gainers_*.json)
# ---------------------------------------------------------------------------


def save_top_gainers(data):
    """Insert a new top-gainers snapshot (JSON list) with a created_at time."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(top_gainers_snapshots),
            {
                "created_at": datetime.now().strftime(TIMESTAMP_FORMAT),
                "data": json.dumps(data),
            },
        )


def get_latest_top_gainers():
    """
    Return the most recent top gainers snapshot.

    Returns:
        tuple[datetime | None, list]: (created_at, data list). Returns
        (None, []) when no snapshot exists.
    """
    engine = get_engine()
    stmt = (
        select(top_gainers_snapshots.c.created_at, top_gainers_snapshots.c.data)
        .order_by(top_gainers_snapshots.c.created_at.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    if not row:
        return None, []
    return datetime.strptime(row[0], TIMESTAMP_FORMAT), json.loads(row[1])


def delete_top_gainers_before(cutoff):
    """Remove top gainers snapshots older than cutoff. Returns rows deleted."""
    cutoff_str = cutoff.strftime(TIMESTAMP_FORMAT)
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            delete(top_gainers_snapshots).where(
                top_gainers_snapshots.c.created_at < cutoff_str
            )
        )
        return result.rowcount or 0
