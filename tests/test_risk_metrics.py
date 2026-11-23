"""
Tests for the accuracy of the risk and correlation figures.

Each test builds a series whose true answer is known, then checks that the
reported number matches it at every data resolution.
"""

from datetime import timedelta

import numpy as np
import pytest

from cryptoviz.db import repository as repo
from cryptoviz.db.models import utc_now
from cryptoviz.services import analytics


def _row(symbol, price, timestamp):
    """Build one price row."""
    return {
        "symbol": symbol,
        "name": symbol.upper(),
        "price": float(price),
        "market_cap": 1.0e9,
        "volume_24h": 1.0e6,
        "percent_change_24h": 0.0,
        "timestamp": timestamp,
    }


def _random_walk(days, daily_volatility, step_minutes=5, seed=7):
    """Simulate prices whose true daily volatility is known, oldest first."""
    steps = int(days * 1440 / step_minutes)
    step_volatility = daily_volatility / np.sqrt(1440 / step_minutes)
    shocks = np.random.default_rng(seed).normal(0, step_volatility, steps)
    return 100 * np.exp(np.cumsum(shocks))


@pytest.mark.parametrize("days", [7, 30, 120])
def test_volatility_is_daily_at_every_resolution(clean_db, days):
    """Raw, hourly and daily windows all report about the true daily figure."""
    prices = _random_walk(days, daily_volatility=0.03)
    end = utc_now().replace(second=0, microsecond=0)
    start = end - timedelta(minutes=5 * (len(prices) - 1))

    repo.save_prices(
        [
            _row("btc", price, start + timedelta(minutes=5 * i))
            for i, price in enumerate(prices)
        ]
    )
    repo.refresh_price_buckets()

    result = analytics.calculate_volatility("btc", days=min(days, 90))

    # A sample of daily closes is noisy, so the tolerance is deliberately wide.
    assert result["volatility"] == pytest.approx(3.0, abs=0.9)


def test_gap_in_collection_does_not_inflate_volatility(clean_db):
    """A return across a long gap is weighted by the time it spans."""
    prices = _random_walk(2, daily_volatility=0.02)
    end = utc_now().replace(second=0, microsecond=0)
    times = [
        end - timedelta(minutes=5 * (len(prices) - 1 - i)) for i in range(len(prices))
    ]
    # Remove the middle half of the series.
    keep = [
        i for i in range(len(prices)) if not len(prices) // 4 < i < 3 * len(prices) // 4
    ]

    repo.save_prices([_row("btc", prices[i], times[i]) for i in keep])

    assert analytics.calculate_volatility("btc", days=7)["volatility"] == pytest.approx(
        2.0, abs=0.8
    )


@pytest.mark.parametrize("days", [30, 120])
def test_bucketed_drawdown_keeps_a_crash_inside_a_bucket(clean_db, days):
    """A 15-minute crash is not averaged away by hourly or daily rollups."""
    end = utc_now().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=5)
    rows = [_row("btc", 100, start + timedelta(hours=h)) for h in range(120)]
    rows.append(_row("btc", 50, start + timedelta(hours=60, minutes=15)))
    repo.save_prices(rows)
    repo.refresh_price_buckets()

    result = analytics.calculate_volatility("btc", days=days)

    assert result["max_drawdown"] == pytest.approx(-50.0)


def test_drawdown_includes_the_first_price(clean_db):
    """The peak can be the very first observation in the window."""
    now = utc_now().replace(microsecond=0)
    repo.save_prices(
        [
            _row("btc", price, now - timedelta(hours=3 - i))
            for i, price in enumerate((100, 50, 60))
        ]
    )

    assert analytics.calculate_volatility("btc", days=1)["max_drawdown"] == -50.0
    assert analytics.get_all_volatility(days=1)[0]["max_drawdown"] == -50.0


def test_volatility_skips_coins_that_left_the_top_list(clean_db):
    """A dropped coin is not shown with its stale price."""
    now = utc_now().replace(microsecond=0)
    old = now - timedelta(hours=2)
    repo.save_prices(
        [
            _row("btc", 100, old),
            _row("btc", 101, now),
            _row("gone", 5, old - timedelta(hours=1)),
            _row("gone", 6, old),
        ]
    )

    assert [row["symbol"] for row in analytics.get_all_volatility(days=1)] == ["btc"]


def test_new_listing_does_not_shrink_other_correlations(clean_db):
    """One coin with few rows cannot truncate the sample for every other pair."""
    rng = np.random.default_rng(3)
    now = utc_now().replace(microsecond=0)
    btc = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 100)))
    eth = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, 100)))
    times = [now - timedelta(minutes=5 * (99 - i)) for i in range(100)]

    rows = [_row("btc", p, t) for p, t in zip(btc, times, strict=True)]
    rows += [_row("eth", p, t) for p, t in zip(eth, times, strict=True)]
    repo.save_prices(rows)
    before = analytics.calculate_correlation_matrix(days=1)
    pair = before["correlation_matrix"][0][1]

    repo.save_prices([_row("new", 3 + i * 0.1, t) for i, t in enumerate(times[-4:])])
    after = analytics.calculate_correlation_matrix(days=1)

    assert "new" not in after["symbols"]
    assert after["correlation_matrix"][0][1] == pair


def test_calm_real_asset_stays_in_correlation(clean_db):
    """An asset far from $1 is not mistaken for a stablecoin because it is calm."""
    rng = np.random.default_rng(5)
    now = utc_now().replace(microsecond=0)
    times = [now - timedelta(minutes=5 * (49 - i)) for i in range(50)]
    btc = 60000 * (1 + rng.normal(0, 0.0001, 50))
    eth = 3000 * (1 + rng.normal(0, 0.0001, 50))

    repo.save_prices(
        [_row("btc", p, t) for p, t in zip(btc, times, strict=True)]
        + [_row("eth", p, t) for p, t in zip(eth, times, strict=True)]
    )

    assert analytics.calculate_correlation_matrix(days=1)["symbols"] == ["btc", "eth"]


def test_rollup_close_is_the_last_price(clean_db):
    """Buckets keep the real last, lowest and highest prices."""
    hour = (utc_now() - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    repo.save_prices(
        [
            _row("btc", price, hour + timedelta(minutes=minute))
            for minute, price in ((0, 100.0), (10, 300.0), (20, 50.0), (30, 120.5))
        ]
    )

    for _ in range(2):  # Once from raw rows, then from the built rollup.
        row = repo.get_prices_df("btc", bucket="hour").iloc[0]
        assert row["close"] == pytest.approx(120.5)
        assert row["low"] == pytest.approx(50.0)
        assert row["high"] == pytest.approx(300.0)
        repo.refresh_price_buckets()


def test_rollup_and_fallback_agree_at_the_window_edge(clean_db):
    """A `since` inside a bucket selects the same buckets on both read paths."""
    hour = (utc_now() - timedelta(hours=5)).replace(minute=0, second=0, microsecond=0)
    repo.save_prices(
        [_row("btc", 100 + i, hour + timedelta(minutes=10 * i)) for i in range(24)]
    )
    since = hour + timedelta(minutes=35)

    fallback = repo.get_prices_df("btc", since=since, bucket="hour")
    repo.refresh_price_buckets()
    from_rollup = repo.get_prices_df("btc", since=since, bucket="hour")

    assert len(fallback) == len(from_rollup)


def test_partial_batch_keeps_other_coins(clean_db):
    """A newer row for one coin does not hide the rest of the last scrape."""
    now = utc_now().replace(microsecond=0)
    repo.save_prices(
        [
            _row("btc", 100, now - timedelta(minutes=5)),
            _row("eth", 50, now - timedelta(minutes=5)),
            _row("btc", 101, now),
        ]
    )

    rows = {row["symbol"]: row["price"] for row in repo.get_latest_prices()}

    assert rows == {"btc": 101.0, "eth": 50.0}


def test_long_trend_window_is_downsampled(clean_db):
    """A long sentiment window reads one snapshot per bucket."""
    from sqlalchemy import insert

    from cryptoviz.db.engine import get_engine
    from cryptoviz.db.models import TIMESTAMP_FORMAT, sentiment_snapshots

    now = utc_now().replace(microsecond=0)
    rows = [
        {
            "created_at": (now - timedelta(minutes=30 * i)).strftime(TIMESTAMP_FORMAT),
            "data": '{"overall": {"sentiment": 40.0}}',
        }
        for i in range(96)
    ]
    with get_engine().begin() as conn:
        conn.execute(insert(sentiment_snapshots), rows)

    cutoff = now - timedelta(days=30)

    assert len(repo.get_sentiment_snapshots_since(cutoff)) == 96
    assert len(repo.get_sentiment_snapshots_since(cutoff, bucket="day")) <= 3
    assert repo.get_sentiment_averages(cutoff, ["sentiment"])["sentiment"] == 40.0
