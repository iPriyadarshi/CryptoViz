"""
Tests for the pre-aggregated price rollups.

The rollup exists for speed, but it is only safe if it stays numerically
identical to aggregating the raw rows. These tests pin that equivalence, the
fallback when no rollup exists, and the incremental refresh.
"""

from datetime import datetime, timedelta

import pytest

from cryptoviz.db import repository as repo


@pytest.fixture
def two_days_of_prices():
    """Two days of 5-minute rows for two symbols, with a varying price."""
    start = (datetime.now() - timedelta(days=2)).replace(second=0, microsecond=0)
    rows = []
    for i in range(576):
        ts = start + timedelta(minutes=5 * i)
        rows.append(
            {
                "symbol": "btc",
                "name": "Bitcoin",
                "price": 60000.0 + i,
                "market_cap": 1.2e12,
                "volume_24h": 3.0e10,
                "percent_change_24h": 1.0,
                "timestamp": ts,
            }
        )
        rows.append(
            {
                "symbol": "eth",
                "name": "Ethereum",
                "price": 3000.0 - i * 0.5,
                "market_cap": 4.0e11,
                "volume_24h": 1.0e10,
                "percent_change_24h": -0.5,
                "timestamp": ts,
            }
        )
    return rows


def test_no_buckets_until_refreshed(clean_db, two_days_of_prices):
    """Saving prices does not populate the rollup by itself."""
    repo.save_prices(two_days_of_prices)

    assert repo.count_price_buckets() == 0
    assert repo.has_price_buckets("day") is False


def test_refresh_populates_both_granularities(clean_db, two_days_of_prices):
    """A refresh writes hourly and daily buckets."""
    repo.save_prices(two_days_of_prices)

    written = repo.refresh_price_buckets()

    assert written["hour"] > 0
    assert written["day"] > 0
    assert repo.has_price_buckets("hour") is True
    assert repo.has_price_buckets("day") is True
    # Two symbols over ~48 hours.
    assert repo.count_price_buckets("hour") <= 2 * 49
    assert repo.count_price_buckets("day") <= 2 * 3


def test_rollup_matches_on_the_fly_aggregation(clean_db, two_days_of_prices):
    """
    Reading the rollup gives the same numbers as aggregating raw rows.

    This is the correctness guarantee that makes the rollup a pure optimization:
    the same call returns the same values whether or not it has been built.
    """
    repo.save_prices(two_days_of_prices)

    # Before any refresh this falls back to aggregating the raw rows.
    fallback = repo.get_prices_df("btc", bucket="day").sort_values("timestamp")

    repo.refresh_price_buckets()

    # After the refresh it reads the stored buckets.
    from_rollup = repo.get_prices_df("btc", bucket="day").sort_values("timestamp")

    assert len(fallback) == len(from_rollup)
    assert fallback["timestamp"].tolist() == from_rollup["timestamp"].tolist()
    for a, b in zip(fallback["price"], from_rollup["price"], strict=True):
        assert a == pytest.approx(b)


def test_hourly_rollup_matches_fallback(clean_db, two_days_of_prices):
    """The same equivalence holds at hourly granularity."""
    repo.save_prices(two_days_of_prices)
    fallback = repo.get_prices_df(bucket="hour").sort_values(["symbol", "timestamp"])

    repo.refresh_price_buckets()
    from_rollup = repo.get_prices_df(bucket="hour").sort_values(["symbol", "timestamp"])

    assert len(fallback) == len(from_rollup)
    for a, b in zip(fallback["price"], from_rollup["price"], strict=True):
        assert a == pytest.approx(b)


def test_bucket_price_is_the_average_of_its_rows(clean_db):
    """A rollup bucket holds the mean of the raw rows inside it."""
    hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    repo.save_prices(
        [
            {
                "symbol": "btc",
                "name": "Bitcoin",
                "price": price,
                "market_cap": 1.0,
                "volume_24h": 1.0,
                "percent_change_24h": 0.0,
                "timestamp": hour + timedelta(minutes=minute),
            }
            for minute, price in ((0, 100.0), (5, 200.0), (10, 300.0))
        ]
    )
    repo.refresh_price_buckets()

    df = repo.get_prices_df("btc", bucket="hour")

    assert len(df) == 1
    assert df.iloc[0]["price"] == pytest.approx(200.0)


def test_refresh_is_idempotent(clean_db, two_days_of_prices):
    """Refreshing twice does not duplicate buckets."""
    repo.save_prices(two_days_of_prices)

    repo.refresh_price_buckets()
    first = repo.count_price_buckets()
    repo.refresh_price_buckets()

    assert repo.count_price_buckets() == first


def test_incremental_refresh_picks_up_new_rows(clean_db, two_days_of_prices):
    """
    A trailing refresh updates the buckets covering new data.

    This is the path the worker takes after every scrape.
    """
    repo.save_prices(two_days_of_prices)
    repo.refresh_price_buckets()

    hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    # A new, distinctly-priced reading in the current hour.
    repo.save_prices(
        [
            {
                "symbol": "btc",
                "name": "Bitcoin",
                "price": 99999.0,
                "market_cap": 1.0,
                "volume_24h": 1.0,
                "percent_change_24h": 0.0,
                "timestamp": hour + timedelta(minutes=55),
            }
        ]
    )

    repo.refresh_price_buckets(since=datetime.now() - timedelta(days=2))

    df = repo.get_prices_df("btc", bucket="hour").sort_values("timestamp")
    # The newest bucket reflects the new reading.
    assert df.iloc[-1]["price"] > 60600


def test_incremental_refresh_leaves_older_buckets_intact(clean_db):
    """A trailing refresh does not disturb buckets outside its window."""
    old = datetime.now() - timedelta(days=30)
    repo.save_prices(
        [
            {
                "symbol": "btc",
                "name": "Bitcoin",
                "price": 100.0,
                "market_cap": 1.0,
                "volume_24h": 1.0,
                "percent_change_24h": 0.0,
                "timestamp": old,
            }
        ]
    )
    repo.refresh_price_buckets()
    before = repo.count_price_buckets("day")

    # Refresh only the last two days; the 30-day-old bucket must survive.
    repo.refresh_price_buckets(since=datetime.now() - timedelta(days=2))

    assert repo.count_price_buckets("day") == before


def test_retention_prunes_buckets(clean_db, two_days_of_prices):
    """Bucket cleanup removes rows older than the cutoff."""
    repo.save_prices(two_days_of_prices)
    repo.refresh_price_buckets()
    assert repo.count_price_buckets() > 0

    repo.delete_price_buckets_before(datetime.now() + timedelta(days=1))

    assert repo.count_price_buckets() == 0


def test_cleanup_prunes_buckets_with_raw_rows(clean_db, two_days_of_prices):
    """
    Retention drops rollups alongside the raw rows they summarize.

    A bucket outliving its source data would keep charting history the raw
    table no longer has.
    """
    from cryptoviz import config
    from cryptoviz.services import analytics

    # Place everything beyond the retention window.
    stale = datetime.now() - timedelta(days=config.PRICE_RETENTION_DAYS + 10)
    repo.save_prices(
        [
            {
                "symbol": "btc",
                "name": "Bitcoin",
                "price": 100.0,
                "market_cap": 1.0,
                "volume_24h": 1.0,
                "percent_change_24h": 0.0,
                "timestamp": stale,
            }
        ]
    )
    repo.refresh_price_buckets()
    assert repo.count_price_buckets() > 0

    analytics.cleanup_old_data()

    assert repo.count_prices() == 0
    assert repo.count_price_buckets() == 0


def test_symbol_filter_on_rollup(clean_db, two_days_of_prices):
    """Filtering by symbol works against the rollup table."""
    repo.save_prices(two_days_of_prices)
    repo.refresh_price_buckets()

    df = repo.get_prices_df("eth", bucket="day")

    assert not df.empty
    assert set(df["symbol"].unique()) == {"eth"}


def test_since_filter_on_rollup(clean_db, two_days_of_prices):
    """The since filter narrows rollup reads."""
    repo.save_prices(two_days_of_prices)
    repo.refresh_price_buckets()

    everything = repo.get_prices_df(bucket="hour")
    recent = repo.get_prices_df(bucket="hour", since=datetime.now() - timedelta(hours=6))

    assert not recent.empty
    assert len(recent) < len(everything)
