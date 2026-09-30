"""
Database schema definitions for CryptoViz.

Six tables back the platform:

    crypto_prices             normalized time-series price rows, queried by the
                              analytics layer for history, volatility and
                              correlation
    price_buckets             hourly and daily averages of crypto_prices, kept
                              up to date by the worker so long-range charts do
                              not re-aggregate a year of rows on every request
    sentiment_snapshots       one row per sentiment scrape, holding only the
                              aggregate readings (overall, per-crypto scores,
                              rankings)
    sentiment_sources         the articles and posts behind those scores, stored
                              once each and shared across every snapshot that
                              saw them
    sentiment_source_mentions which cryptocurrencies each source mentions
    top_gainers_snapshots     one row per top-gainers scrape; the JSON list
                              stored verbatim with a created_at timestamp

Why sources are a table rather than part of the snapshot blob: successive
scrapes re-read the same front pages, so the same article reappears in snapshot
after snapshot. Embedding them meant a year of history was several GB of
near-duplicate JSON that had to be loaded into Python and de-duplicated on every
request. Stored once and keyed by URL, a year of articles is a few tens of
thousands of rows that can be filtered in SQL.

Timestamps are stored as text in TIMESTAMP_FORMAT rather than as a native date
type. The format is zero-padded and most-significant-first, so lexicographic
comparison is also chronological comparison - that is what lets the repository
filter by time range with a plain string comparison, and what lets a prefix of
the string act as a truncation to the hour or the day.
"""

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

# Timestamp format used consistently throughout the application.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Character counts that truncate a TIMESTAMP_FORMAT string to a coarser unit.
# "2026-01-31 14:05:00" -> [:13] is the hour, [:10] is the day.
HOUR_PREFIX_LENGTH = 13
DAY_PREFIX_LENGTH = 10

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
    Column("timestamp", String(32), nullable=False),
    Index("ix_crypto_prices_symbol_ts", "symbol", "timestamp"),
    Index("ix_crypto_prices_ts", "timestamp"),
)

# Indexes on the truncated timestamp, matching the GROUP BY that builds the
# rollup buckets. Without them SQLite scans and sorts the table to group it;
# with them it walks the index in group order instead. These matter for the
# refresh that maintains price_buckets, and for the fallback path that
# aggregates directly when no buckets exist yet. They are declared separately
# from the Table because they reference its columns.
# The truncated timestamp leads, with symbol second. That order matters: the
# incremental refresh filters on the bucket expression, and only a leading
# expression column turns that filter into a range SEARCH. With symbol first,
# SQLite has to SCAN all ~1,000,000 index entries to emit the few hundred recent
# buckets - measured at 774ms versus 3ms.
Index(
    "ix_crypto_prices_day_symbol",
    func.substr(crypto_prices.c.timestamp, 1, DAY_PREFIX_LENGTH),
    crypto_prices.c.symbol,
)
Index(
    "ix_crypto_prices_hour_symbol",
    func.substr(crypto_prices.c.timestamp, 1, HOUR_PREFIX_LENGTH),
    crypto_prices.c.symbol,
)

price_buckets = Table(
    "price_buckets",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # 'hour' or 'day'; both granularities share the table.
    Column("granularity", String(8), nullable=False),
    Column("symbol", String(32), nullable=False),
    # Start of the bucket, in TIMESTAMP_FORMAT.
    Column("timestamp", String(32), nullable=False),
    Column("name", String(128)),
    Column("price", Float),
    Column("market_cap", Float),
    Column("volume_24h", Float),
    Column("percent_change_24h", Float),
    UniqueConstraint(
        "granularity", "symbol", "timestamp", name="uq_price_buckets_identity"
    ),
    Index("ix_price_buckets_lookup", "granularity", "symbol", "timestamp"),
    Index("ix_price_buckets_granularity_ts", "granularity", "timestamp"),
)

sentiment_snapshots = Table(
    "sentiment_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", String(32), nullable=False),
    # Aggregate readings only: overall, crypto_specific scores and rankings.
    # The articles themselves live in sentiment_sources.
    Column("data", Text, nullable=False),
    Index("ix_sentiment_created_at", "created_at"),
)

sentiment_sources = Table(
    "sentiment_sources",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Stable identity for a source: its URL, falling back to its title when the
    # scrape produced no link. Unique, so a repeat sighting updates the existing
    # row instead of inserting a duplicate.
    Column("dedupe_key", String(512), nullable=False),
    Column("url", Text),
    Column("type", String(32)),  # 'news' or 'reddit'
    Column("source", String(128)),  # publication or subreddit
    Column("title", Text),
    Column("sentiment", Float),
    # When the article itself was published, as reported by the source.
    Column("timestamp", String(32)),
    # When this deployment first and most recently saw the article. Retention
    # is applied against last_seen.
    Column("first_seen", String(32), nullable=False),
    Column("last_seen", String(32), nullable=False),
    UniqueConstraint("dedupe_key", name="uq_sentiment_sources_dedupe_key"),
    Index("ix_sentiment_sources_last_seen", "last_seen"),
    Index("ix_sentiment_sources_type", "type"),
    Index("ix_sentiment_sources_timestamp", "timestamp"),
)

sentiment_source_mentions = Table(
    "sentiment_source_mentions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "source_id",
        Integer,
        ForeignKey("sentiment_sources.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("symbol", String(32), nullable=False),
    UniqueConstraint("source_id", "symbol", name="uq_mention_source_symbol"),
    # Filtering sources by cryptocurrency is an indexed lookup on this table.
    Index("ix_mentions_symbol", "symbol"),
)

top_gainers_snapshots = Table(
    "top_gainers_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", String(32), nullable=False),
    Column("data", Text, nullable=False),  # JSON list of top gainer dicts
    Index("ix_top_gainers_created_at", "created_at"),
)
