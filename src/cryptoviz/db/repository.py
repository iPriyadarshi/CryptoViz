"""
Data access layer for CryptoViz.

All SQL lives here; the rest of the application works with plain dicts and
pandas DataFrames and never builds a query of its own.

Because timestamps are stored in a lexicographically sortable text format (see
models.TIMESTAMP_FORMAT), time-range filters are pushed down into SQL as plain
string comparisons, and a prefix of that string acts as a truncation to the hour
or the day - which is how long windows are downsampled without a date function.
"""

import json
import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import delete, func, insert, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .engine import get_engine
from .models import (
    DAY_PREFIX_LENGTH,
    HOUR_PREFIX_LENGTH,
    TIMESTAMP_FORMAT,
    crypto_prices,
    price_buckets,
    sentiment_snapshots,
    sentiment_source_mentions,
    sentiment_sources,
    top_gainers_snapshots,
)

logger = logging.getLogger(__name__)

PRICE_COLUMNS = [
    "symbol",
    "name",
    "price",
    "market_cap",
    "volume_24h",
    "percent_change_24h",
    "timestamp",
]

# Granularities accepted by get_prices_df(bucket=...).
BUCKET_PREFIXES = {
    "hour": HOUR_PREFIX_LENGTH,
    "day": DAY_PREFIX_LENGTH,
}


# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------


def _normalize_timestamp(value):
    """Coerce a timestamp value into the canonical string format."""
    if isinstance(value, datetime):
        return value.strftime(TIMESTAMP_FORMAT)
    if isinstance(value, str):
        # Reformat for consistency; fall back to the raw string if unparseable.
        try:
            return pd.to_datetime(value).strftime(TIMESTAMP_FORMAT)
        except Exception:
            return value
    try:
        return pd.to_datetime(value).strftime(TIMESTAMP_FORMAT)
    except Exception:
        return datetime.now().strftime(TIMESTAMP_FORMAT)


def save_prices(data):
    """
    Persist a batch of cryptocurrency price records.

    Args:
        data (list[dict]): rows containing the PRICE_COLUMNS keys.
    """
    if not data:
        return

    rows = [
        {
            "symbol": entry.get("symbol"),
            "name": entry.get("name"),
            "price": entry.get("price"),
            "market_cap": entry.get("market_cap"),
            "volume_24h": entry.get("volume_24h"),
            "percent_change_24h": entry.get("percent_change_24h"),
            "timestamp": _normalize_timestamp(entry.get("timestamp")),
        }
        for entry in data
    ]

    with get_engine().begin() as conn:
        conn.execute(insert(crypto_prices), rows)


def _bucket_pad(bucket):
    """Return the text that completes a truncated timestamp for this bucket."""
    return ":00:00" if bucket == "hour" else " 00:00:00"


def _aggregate_select(bucket, symbol=None, since=None):
    """
    Build the GROUP BY that averages raw price rows into buckets.

    Used both to populate price_buckets and as the read fallback when that table
    has not been built yet.

    The `since` filter is applied to the *bucket expression*, not to the raw
    timestamp. That is deliberate on two counts: it matches the leading column of
    ix_crypto_prices_{hour,day}_symbol, so the filter is a range search instead
    of a full index scan; and it includes the whole bucket containing `since`
    rather than averaging a partial one.
    """
    prefix_length = BUCKET_PREFIXES[bucket]
    bucket_key = func.substr(crypto_prices.c.timestamp, 1, prefix_length)

    stmt = select(
        crypto_prices.c.symbol,
        func.max(crypto_prices.c.name).label("name"),
        func.avg(crypto_prices.c.price).label("price"),
        func.avg(crypto_prices.c.market_cap).label("market_cap"),
        func.avg(crypto_prices.c.volume_24h).label("volume_24h"),
        func.avg(crypto_prices.c.percent_change_24h).label("percent_change_24h"),
        (bucket_key + _bucket_pad(bucket)).label("timestamp"),
    ).group_by(crypto_prices.c.symbol, bucket_key)

    if symbol is not None:
        stmt = stmt.where(crypto_prices.c.symbol == symbol)
    if since is not None:
        stmt = stmt.where(bucket_key >= since.strftime(TIMESTAMP_FORMAT)[:prefix_length])

    return stmt


def has_price_buckets(granularity):
    """Whether the rollup table holds any rows at this granularity."""
    stmt = (
        select(price_buckets.c.id)
        .where(price_buckets.c.granularity == granularity)
        .limit(1)
    )
    with get_engine().connect() as conn:
        return conn.execute(stmt).first() is not None


def refresh_price_buckets(since=None):
    """
    Recompute the rollup buckets from the raw price rows.

    The worker calls this once at startup for the whole window, then over a
    short trailing window after each scrape. It recomputes rather than
    increments, so a collection gap, a restart, or a late-arriving row cannot
    leave a bucket permanently skewed - each bucket always equals an aggregate
    of the raw rows underneath it.

    Args:
        since (datetime, optional): only rebuild buckets at or after this time.
            Omit to rebuild everything.

    Returns:
        dict[str, int]: rows written per granularity.
    """
    written = {}

    # One transaction per granularity: readers either see the old range or the
    # new one, never a half-rebuilt range.
    for granularity in BUCKET_PREFIXES:
        rebuild_from = None
        delete_stmt = delete(price_buckets).where(
            price_buckets.c.granularity == granularity
        )

        if since is not None:
            # Snap the cutoff down to the start of the bucket it falls in, so
            # that bucket is rebuilt in full instead of being dropped and
            # rebuilt from only part of its rows.
            prefix_length = BUCKET_PREFIXES[granularity]
            cutoff = since.strftime(TIMESTAMP_FORMAT)[:prefix_length] + _bucket_pad(
                granularity
            )
            delete_stmt = delete_stmt.where(price_buckets.c.timestamp >= cutoff)
            rebuild_from = datetime.strptime(cutoff, TIMESTAMP_FORMAT)

        # The granularity is constant for this pass, so it rides along as a
        # literal column in the SELECT: an INSERT ... FROM SELECT cannot also
        # carry .values(), and this keeps the whole rebuild inside SQL with no
        # round-trip through Python.
        source = _aggregate_select(granularity, since=rebuild_from).add_columns(
            literal(granularity).label("granularity")
        )

        with get_engine().begin() as conn:
            conn.execute(delete_stmt)
            result = conn.execute(
                insert(price_buckets).from_select(
                    [
                        "symbol",
                        "name",
                        "price",
                        "market_cap",
                        "volume_24h",
                        "percent_change_24h",
                        "timestamp",
                        "granularity",
                    ],
                    source,
                )
            )
            written[granularity] = result.rowcount or 0

    return written


def count_price_buckets(granularity=None):
    """Count rollup rows, optionally for one granularity."""
    stmt = select(func.count()).select_from(price_buckets)
    if granularity is not None:
        stmt = stmt.where(price_buckets.c.granularity == granularity)
    with get_engine().connect() as conn:
        return conn.execute(stmt).scalar() or 0


def delete_price_buckets_before(cutoff):
    """Remove rollup buckets older than the cutoff. Returns rows deleted."""
    cutoff_str = cutoff.strftime(TIMESTAMP_FORMAT)
    with get_engine().begin() as conn:
        result = conn.execute(
            delete(price_buckets).where(price_buckets.c.timestamp < cutoff_str)
        )
        return result.rowcount or 0


def get_prices_df(symbol=None, since=None, bucket=None):
    """
    Load price rows into a DataFrame with a parsed datetime 'timestamp' column.

    Args:
        symbol (str, optional): restrict to a single symbol.
        since (datetime, optional): restrict to rows at or after this time. The
            filter runs in SQL, so a short window does not read the whole table.
        bucket (str, optional): 'hour' or 'day'. Reads pre-computed averages
            from price_buckets, which the worker maintains - reading ~3,600
            daily rows instead of aggregating ~1,000,000 raw ones is the
            difference between milliseconds and a second. Falls back to
            aggregating on the fly when the rollup has not been built yet, so a
            fresh deployment is correct before the worker's first pass.

    Returns:
        pandas.DataFrame: columns symbol, name, price, market_cap, volume_24h,
        percent_change_24h, timestamp (datetime64). Empty (with the right
        columns) when there is no matching data.
    """
    if bucket and bucket not in BUCKET_PREFIXES:
        raise ValueError(f"Unsupported bucket {bucket!r}; use 'hour' or 'day'.")

    if bucket and has_price_buckets(bucket):
        stmt = select(
            price_buckets.c.symbol,
            price_buckets.c.name,
            price_buckets.c.price,
            price_buckets.c.market_cap,
            price_buckets.c.volume_24h,
            price_buckets.c.percent_change_24h,
            price_buckets.c.timestamp,
        ).where(price_buckets.c.granularity == bucket)

        if symbol is not None:
            stmt = stmt.where(price_buckets.c.symbol == symbol)
        if since is not None:
            stmt = stmt.where(
                price_buckets.c.timestamp >= since.strftime(TIMESTAMP_FORMAT)
            )
    elif bucket:
        logger.debug("No %s buckets available yet; aggregating from raw rows", bucket)
        stmt = _aggregate_select(bucket, symbol=symbol, since=since)
    else:
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
        if since is not None:
            stmt = stmt.where(
                crypto_prices.c.timestamp >= since.strftime(TIMESTAMP_FORMAT)
            )

    df = pd.read_sql(stmt, get_engine())

    if df.empty:
        return pd.DataFrame(columns=PRICE_COLUMNS)

    # Every stored timestamp is written in TIMESTAMP_FORMAT, so parse it
    # explicitly rather than having pandas infer the format per value.
    df["timestamp"] = pd.to_datetime(
        df["timestamp"], format=TIMESTAMP_FORMAT, errors="coerce"
    )
    return df


def count_prices(symbol=None, since=None):
    """Count price rows matching the filters, without loading them."""
    stmt = select(func.count()).select_from(crypto_prices)
    if symbol is not None:
        stmt = stmt.where(crypto_prices.c.symbol == symbol)
    if since is not None:
        stmt = stmt.where(crypto_prices.c.timestamp >= since.strftime(TIMESTAMP_FORMAT))
    with get_engine().connect() as conn:
        return conn.execute(stmt).scalar() or 0


def get_latest_prices():
    """
    Return the price rows belonging to the most recent timestamp.

    Returns:
        list[dict]: one row per cryptocurrency, or [] when there is no data.
    """
    engine = get_engine()
    latest_stmt = (
        select(crypto_prices.c.timestamp)
        .order_by(crypto_prices.c.timestamp.desc())
        .limit(1)
    )

    with engine.connect() as conn:
        latest = conn.execute(latest_stmt).scalar()
        if latest is None:
            return []

        stmt = select(
            crypto_prices.c.symbol,
            crypto_prices.c.name,
            crypto_prices.c.price,
            crypto_prices.c.market_cap,
            crypto_prices.c.volume_24h,
            crypto_prices.c.percent_change_24h,
            crypto_prices.c.timestamp,
        ).where(crypto_prices.c.timestamp == latest)
        rows = conn.execute(stmt).mappings().all()

    return [dict(row) for row in rows]


def get_tracked_symbols(since=None):
    """Return the distinct symbols present in the price table."""
    stmt = select(crypto_prices.c.symbol).distinct()
    if since is not None:
        stmt = stmt.where(crypto_prices.c.timestamp >= since.strftime(TIMESTAMP_FORMAT))
    with get_engine().connect() as conn:
        return [row[0] for row in conn.execute(stmt).all()]


def delete_prices_before(cutoff):
    """Remove price rows older than the cutoff datetime. Returns rows deleted."""
    cutoff_str = cutoff.strftime(TIMESTAMP_FORMAT)
    with get_engine().begin() as conn:
        result = conn.execute(
            delete(crypto_prices).where(crypto_prices.c.timestamp < cutoff_str)
        )
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# Sentiment snapshots and sources
# ---------------------------------------------------------------------------


def _dedupe_key(source):
    """
    Build the stable identity for a source row.

    Prefers the URL; falls back to the title so that sources scraped without a
    link still de-duplicate instead of inserting a copy per scrape.
    """
    url = (source.get("url") or "").strip()
    if url:
        return url[:512]
    title = (source.get("title") or "").strip()
    return f"title:{title}"[:512] if title else ""


def _split_snapshot(sentiment_data):
    """
    Separate a scrape result into its aggregate part and its source list.

    The aggregates are small and differ every scrape, so they stay in the
    snapshot. The sources are large and mostly repeat, so they are stored once
    in their own table.

    Returns:
        tuple[dict, list]: (aggregate data to store, sources to upsert)
    """
    sources = sentiment_data.get("sources") or []

    aggregates = {
        "overall": sentiment_data.get("overall", {}),
        "rankings": sentiment_data.get("rankings", {"positive": [], "negative": []}),
        "crypto_specific": {},
    }

    # Keep the per-crypto scores (the trends endpoint averages them) but drop
    # the embedded source lists, which duplicate titles already stored above.
    for symbol, data in (sentiment_data.get("crypto_specific") or {}).items():
        aggregates["crypto_specific"][symbol] = {
            "sentiment_scores": data.get("sentiment_scores", []),
            "sources_count": len(data.get("sources", [])),
        }

    return aggregates, sources


def save_sentiment_snapshot(sentiment_data):
    """
    Store a sentiment scrape.

    The aggregate readings become a snapshot row; the articles and posts are
    upserted into sentiment_sources, so a source seen in many scrapes occupies
    one row whose last_seen advances.
    """
    now = datetime.now().strftime(TIMESTAMP_FORMAT)
    aggregates, sources = _split_snapshot(sentiment_data)

    with get_engine().begin() as conn:
        conn.execute(
            insert(sentiment_snapshots),
            {"created_at": now, "data": json.dumps(aggregates)},
        )

        for source in sources:
            key = _dedupe_key(source)
            if not key:
                continue  # Nothing stable to identify this source by.

            row = {
                "dedupe_key": key,
                "url": source.get("url"),
                "type": source.get("type"),
                "source": source.get("source"),
                "title": source.get("title"),
                "sentiment": source.get("sentiment"),
                "timestamp": source.get("timestamp") or now,
                "first_seen": now,
                "last_seen": now,
            }

            # Seen before: refresh the score and advance last_seen, leaving
            # first_seen as the original sighting.
            stmt = sqlite_insert(sentiment_sources).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=["dedupe_key"],
                set_={
                    "last_seen": stmt.excluded.last_seen,
                    "sentiment": stmt.excluded.sentiment,
                    "title": stmt.excluded.title,
                },
            )
            conn.execute(stmt)

            source_id = conn.execute(
                select(sentiment_sources.c.id).where(
                    sentiment_sources.c.dedupe_key == key
                )
            ).scalar()
            if source_id is None:
                continue

            # Mentions are a set per source; ignore repeats on re-sighting.
            for symbol in source.get("mentioned_cryptos") or []:
                conn.execute(
                    sqlite_insert(sentiment_source_mentions)
                    .values(source_id=source_id, symbol=str(symbol).upper())
                    .on_conflict_do_nothing(index_elements=["source_id", "symbol"])
                )


def get_latest_sentiment(source_limit=50):
    """
    Return the most recent sentiment reading, with its sources reattached.

    Args:
        source_limit (int): how many recent sources to include.

    Returns:
        dict | None: the same shape the scraper produces, or None when no
        snapshot exists.
    """
    stmt = (
        select(sentiment_snapshots.c.data)
        .order_by(sentiment_snapshots.c.created_at.desc())
        .limit(1)
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt).first()
    if not row:
        return None

    data = json.loads(row[0])
    data["sources"] = query_sentiment_sources(limit=source_limit)
    return data


def get_latest_sentiment_time():
    """Return the datetime of the most recent sentiment snapshot, or None."""
    stmt = (
        select(sentiment_snapshots.c.created_at)
        .order_by(sentiment_snapshots.c.created_at.desc())
        .limit(1)
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt).first()
    if not row:
        return None
    return datetime.strptime(row[0], TIMESTAMP_FORMAT)


def get_sentiment_snapshots_since(cutoff):
    """
    Return sentiment snapshots created on/after cutoff, oldest first.

    Sources are not attached: callers in this range are plotting aggregate
    trends, and reattaching sources per snapshot is what the normalization was
    meant to avoid.

    Returns:
        list[tuple[datetime, dict]]: (created_at, aggregate data)
    """
    cutoff_str = cutoff.strftime(TIMESTAMP_FORMAT)
    stmt = (
        select(sentiment_snapshots.c.created_at, sentiment_snapshots.c.data)
        .where(sentiment_snapshots.c.created_at >= cutoff_str)
        .order_by(sentiment_snapshots.c.created_at.asc())
    )
    with get_engine().connect() as conn:
        rows = conn.execute(stmt).all()

    result = []
    for created_at, data in rows:
        try:
            result.append(
                (datetime.strptime(created_at, TIMESTAMP_FORMAT), json.loads(data))
            )
        except Exception:
            continue
    return result


def query_sentiment_sources(since=None, source_type=None, crypto=None, limit=None):
    """
    Query stored sources with every filter applied in SQL.

    This replaces loading every snapshot in a range and de-duplicating in
    Python: the rows are already unique, and the filters are indexed.

    Args:
        since (datetime, optional): only sources seen at or after this time.
        source_type (str, optional): 'news' or 'reddit'.
        crypto (str, optional): only sources mentioning this symbol.
        limit (int, optional): maximum rows to return.

    Returns:
        list[dict]: newest first, shaped like the scraper's source dicts.
    """
    stmt = select(
        sentiment_sources.c.id,
        sentiment_sources.c.type,
        sentiment_sources.c.source,
        sentiment_sources.c.title,
        sentiment_sources.c.url,
        sentiment_sources.c.sentiment,
        sentiment_sources.c.timestamp,
    )

    if since is not None:
        stmt = stmt.where(
            sentiment_sources.c.last_seen >= since.strftime(TIMESTAMP_FORMAT)
        )
    if source_type:
        stmt = stmt.where(sentiment_sources.c.type == source_type)
    if crypto:
        stmt = stmt.where(
            sentiment_sources.c.id.in_(
                select(sentiment_source_mentions.c.source_id).where(
                    sentiment_source_mentions.c.symbol == str(crypto).upper()
                )
            )
        )

    stmt = stmt.order_by(sentiment_sources.c.timestamp.desc())
    if limit and limit > 0:
        stmt = stmt.limit(limit)

    with get_engine().connect() as conn:
        rows = conn.execute(stmt).mappings().all()
        if not rows:
            return []

        # Fetch the mentions for exactly the rows being returned.
        ids = [row["id"] for row in rows]
        mention_rows = conn.execute(
            select(
                sentiment_source_mentions.c.source_id,
                sentiment_source_mentions.c.symbol,
            ).where(sentiment_source_mentions.c.source_id.in_(ids))
        ).all()

    mentions = {}
    for source_id, symbol in mention_rows:
        mentions.setdefault(source_id, []).append(symbol)

    return [
        {
            "type": row["type"],
            "source": row["source"],
            "title": row["title"],
            "url": row["url"],
            "sentiment": row["sentiment"],
            "timestamp": row["timestamp"],
            "mentioned_cryptos": mentions.get(row["id"], []),
        }
        for row in rows
    ]


def count_sentiment_sources(since=None, source_type=None, crypto=None):
    """Count sources matching the filters, without loading them."""
    stmt = select(func.count()).select_from(sentiment_sources)

    if since is not None:
        stmt = stmt.where(
            sentiment_sources.c.last_seen >= since.strftime(TIMESTAMP_FORMAT)
        )
    if source_type:
        stmt = stmt.where(sentiment_sources.c.type == source_type)
    if crypto:
        stmt = stmt.where(
            sentiment_sources.c.id.in_(
                select(sentiment_source_mentions.c.source_id).where(
                    sentiment_source_mentions.c.symbol == str(crypto).upper()
                )
            )
        )

    with get_engine().connect() as conn:
        return conn.execute(stmt).scalar() or 0


def delete_sentiment_before(cutoff):
    """
    Remove sentiment data older than cutoff.

    Drops both the snapshots and any source not seen since that time, along
    with the mention rows belonging to those sources.

    Returns:
        int: number of snapshot rows removed.
    """
    cutoff_str = cutoff.strftime(TIMESTAMP_FORMAT)

    with get_engine().begin() as conn:
        removed = (
            conn.execute(
                delete(sentiment_snapshots).where(
                    sentiment_snapshots.c.created_at < cutoff_str
                )
            ).rowcount
            or 0
        )

        stale = select(sentiment_sources.c.id).where(
            sentiment_sources.c.last_seen < cutoff_str
        )
        # Mentions are removed explicitly: SQLite only enforces ON DELETE
        # CASCADE when foreign keys are enabled, and this keeps the cleanup
        # correct regardless.
        conn.execute(
            delete(sentiment_source_mentions).where(
                sentiment_source_mentions.c.source_id.in_(stale)
            )
        )
        conn.execute(
            delete(sentiment_sources).where(sentiment_sources.c.last_seen < cutoff_str)
        )

    return removed


# ---------------------------------------------------------------------------
# Top gainers data
# ---------------------------------------------------------------------------


def save_top_gainers(data):
    """Insert a new top-gainers snapshot (JSON list) with a created_at time."""
    with get_engine().begin() as conn:
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
        tuple[datetime | None, list]: (created_at, data list); (None, []) when
        no snapshot exists.
    """
    stmt = (
        select(top_gainers_snapshots.c.created_at, top_gainers_snapshots.c.data)
        .order_by(top_gainers_snapshots.c.created_at.desc())
        .limit(1)
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt).first()
    if not row:
        return None, []
    return datetime.strptime(row[0], TIMESTAMP_FORMAT), json.loads(row[1])


def delete_top_gainers_before(cutoff):
    """Remove top gainers snapshots older than cutoff. Returns rows deleted."""
    cutoff_str = cutoff.strftime(TIMESTAMP_FORMAT)
    with get_engine().begin() as conn:
        result = conn.execute(
            delete(top_gainers_snapshots).where(
                top_gainers_snapshots.c.created_at < cutoff_str
            )
        )
        return result.rowcount or 0
