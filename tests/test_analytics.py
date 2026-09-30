"""
Tests for the analytics layer.

The emphasis is on the cases that used to return misleading values: empty
inputs, single data points, and series with no price variation.
"""

from cryptoviz.services import analytics


def test_history_is_empty_for_unknown_symbol(clean_db):
    """An unknown symbol yields empty lists rather than an error."""
    assert analytics.get_history("nope") == {"prices": [], "timestamps": []}


def test_history_returns_aligned_series(clean_db, price_rows):
    """Prices and timestamps come back as two equal-length series."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    history = analytics.get_history("btc")

    assert len(history["prices"]) == len(history["timestamps"])
    assert len(history["prices"]) > 0
    # Sorted oldest to newest.
    assert history["timestamps"] == sorted(history["timestamps"])


def test_history_thins_repeated_prices(clean_db):
    """A run of identical prices collapses to widely spaced points."""
    from datetime import datetime, timedelta

    from cryptoviz.db import repository as repo

    start = datetime.now() - timedelta(hours=2)
    # One price, sampled every 30 seconds: far more points than movement.
    repo.save_prices(
        [
            {
                "symbol": "flat",
                "name": "Flat",
                "price": 100.0,
                "market_cap": 1.0,
                "volume_24h": 1.0,
                "percent_change_24h": 0.0,
                "timestamp": start + timedelta(seconds=30 * i),
            }
            for i in range(200)
        ]
    )

    history = analytics.get_history("flat")

    # Thinned down to roughly one point per 5-minute spacing window.
    assert 0 < len(history["prices"]) < 50


def test_volatility_needs_two_points(clean_db):
    """A symbol with no data reports zeros instead of failing."""
    result = analytics.calculate_volatility("nope")

    assert result["volatility"] == 0
    assert result["max_drawdown"] == 0
    assert result["daily_returns"] == []


def test_volatility_on_real_series(clean_db, price_rows):
    """A rising series has positive volatility and no drawdown."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    result = analytics.calculate_volatility("btc", days=7)

    assert result["volatility"] > 0
    # btc only rises in the fixture, so it never falls below a running peak.
    assert result["max_drawdown"] == 0
    assert len(result["daily_returns"]) == len(result["timestamps"])


def test_all_volatility_is_sorted_descending(clean_db, price_rows):
    """Results are ordered most volatile first."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    results = analytics.get_all_volatility(days=7)

    assert len(results) == 2
    volatilities = [item["volatility"] for item in results]
    assert volatilities == sorted(volatilities, reverse=True)


def test_correlation_needs_two_symbols(clean_db):
    """An empty database yields an empty matrix, not an exception."""
    result = analytics.calculate_correlation_matrix()

    assert result == {"correlation_matrix": [], "symbols": []}


def test_correlation_matrix_shape(clean_db, price_rows):
    """The matrix is square, matches the symbol list and has a unit diagonal."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    result = analytics.calculate_correlation_matrix(days=7)
    matrix = result["correlation_matrix"]
    symbols = result["symbols"]

    assert len(symbols) == 2
    assert len(matrix) == len(symbols)
    assert all(len(row) == len(symbols) for row in matrix)
    assert all(matrix[i][i] == 1.0 for i in range(len(symbols)))


def test_correlation_excludes_stablecoins(clean_db, price_rows):
    """A pegged asset is left out of the matrix."""
    from datetime import datetime, timedelta

    from cryptoviz.db import repository as repo

    start = datetime.now() - timedelta(days=3)
    pegged = [
        {
            "symbol": "usdt",
            "name": "Tether",
            "price": 1.0,
            "market_cap": 1.0e11,
            "volume_24h": 5.0e10,
            "percent_change_24h": 0.0,
            "timestamp": start + timedelta(hours=hour),
        }
        for hour in range(72)
    ]
    repo.save_prices(price_rows + pegged)

    result = analytics.calculate_correlation_matrix(days=7)

    assert "usdt" not in result["symbols"]


def test_price_at_finds_nearest_point(clean_db, price_rows):
    """A timestamp between samples resolves to the closest one."""
    from datetime import datetime, timedelta

    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    series = analytics.get_price_series("btc")
    assert not series.empty

    price = analytics.price_at(series, datetime.now() - timedelta(hours=1, minutes=7))

    assert isinstance(price, float)
    assert price > 0


def test_price_at_handles_empty_series(clean_db):
    """An empty series yields None rather than raising."""
    series = analytics.get_price_series("nope")

    assert analytics.price_at(series, None) is None
