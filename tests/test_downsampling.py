"""
Tests for long-window downsampling.

With a year of 5-minute data retained, an un-bucketed long query would return
~105,000 rows per symbol. These tests pin that long windows collapse to a
readable series and that the bucket boundaries behave.
"""

from datetime import datetime, timedelta

import pytest

from cryptoviz import config
from cryptoviz.db import repository as repo
from cryptoviz.services import analytics


@pytest.fixture
def dense_rows():
    """Two days of 5-minute price rows for one symbol: 576 points."""
    start = datetime.now() - timedelta(days=2)
    return [
        {
            "symbol": "btc",
            "name": "Bitcoin",
            "price": 60000 + i,
            "market_cap": 1.2e12,
            "volume_24h": 3.0e10,
            "percent_change_24h": 1.0,
            "timestamp": start + timedelta(minutes=5 * i),
        }
        for i in range(576)
    ]


def test_bucket_selection_by_window_size():
    """Short windows stay raw; longer ones step up to hourly then daily."""
    assert analytics.bucket_for_window(1) is None
    assert analytics.bucket_for_window(config.HOURLY_BUCKET_THRESHOLD_DAYS) is None
    assert analytics.bucket_for_window(config.HOURLY_BUCKET_THRESHOLD_DAYS + 1) == "hour"
    assert analytics.bucket_for_window(config.DAILY_BUCKET_THRESHOLD_DAYS) == "hour"
    assert analytics.bucket_for_window(config.DAILY_BUCKET_THRESHOLD_DAYS + 1) == "day"
    assert analytics.bucket_for_window(365) == "day"


def test_hourly_bucket_collapses_rows(clean_db, dense_rows):
    """576 five-minute rows become at most one row per hour."""
    repo.save_prices(dense_rows)

    raw = repo.get_prices_df("btc")
    hourly = repo.get_prices_df("btc", bucket="hour")

    assert len(raw) == 576
    # Two days spans 49 hour boundaries at most.
    assert len(hourly) <= 49
    assert len(hourly) < len(raw)


def test_daily_bucket_collapses_further(clean_db, dense_rows):
    """Daily bucketing yields one row per calendar day."""
    repo.save_prices(dense_rows)

    daily = repo.get_prices_df("btc", bucket="day")

    assert len(daily) <= 3  # two days can touch three calendar dates
    # Timestamps are normalized to midnight of their day.
    assert all(ts.hour == 0 and ts.minute == 0 for ts in daily["timestamp"])


def test_bucketed_price_is_the_bucket_average(clean_db):
    """A bucket's price is the mean of the rows inside it."""
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

    hourly = repo.get_prices_df("btc", bucket="hour")

    assert len(hourly) == 1
    assert hourly.iloc[0]["price"] == pytest.approx(200.0)


def test_bucketed_frame_keeps_the_standard_columns(clean_db, dense_rows):
    """A bucketed frame is shaped like an un-bucketed one."""
    repo.save_prices(dense_rows)

    hourly = repo.get_prices_df("btc", bucket="hour")

    for column in ("symbol", "name", "price", "timestamp"):
        assert column in hourly.columns
    assert hourly["timestamp"].dtype.kind == "M"
    assert set(hourly["symbol"].unique()) == {"btc"}


def test_invalid_bucket_is_rejected(clean_db):
    """An unsupported granularity fails loudly rather than silently ignoring."""
    with pytest.raises(ValueError):
        repo.get_prices_df("btc", bucket="fortnight")


def test_history_downsamples_long_windows(clean_db, dense_rows):
    """A long history request returns a manageable number of points."""
    repo.save_prices(dense_rows)

    short = analytics.get_history("btc", days=1)
    long = analytics.get_history("btc", days=365)

    assert len(short["prices"]) > 0
    assert len(long["prices"]) > 0
    # A year-long view of this data collapses to a handful of daily points.
    assert len(long["prices"]) <= 3


def test_bucketing_preserves_multiple_symbols(clean_db):
    """Bucketing groups per symbol, not across them."""
    start = datetime.now() - timedelta(hours=6)
    rows = []
    for i in range(72):
        ts = start + timedelta(minutes=5 * i)
        rows.append(
            {
                "symbol": "btc",
                "name": "Bitcoin",
                "price": 60000.0,
                "market_cap": 1.0,
                "volume_24h": 1.0,
                "percent_change_24h": 0.0,
                "timestamp": ts,
            }
        )
        rows.append(
            {
                "symbol": "eth",
                "name": "Ethereum",
                "price": 3000.0,
                "market_cap": 1.0,
                "volume_24h": 1.0,
                "percent_change_24h": 0.0,
                "timestamp": ts,
            }
        )
    repo.save_prices(rows)

    hourly = repo.get_prices_df(bucket="hour")

    assert set(hourly["symbol"].unique()) == {"btc", "eth"}
    # Each symbol keeps its own price through the aggregation.
    assert hourly[hourly["symbol"] == "btc"]["price"].iloc[0] == pytest.approx(60000.0)
    assert hourly[hourly["symbol"] == "eth"]["price"].iloc[0] == pytest.approx(3000.0)
