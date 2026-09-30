"""
Tests for the data access layer.

These cover the behaviour the rest of the application depends on: that
timestamps round-trip through the text format, that the `since` filter really
narrows the result, and that retention deletes only what it should.
"""

from datetime import datetime, timedelta

from cryptoviz.db import repository as repo


def test_save_and_read_prices(clean_db, price_rows):
    """Saved rows come back with a parsed datetime timestamp."""
    repo.save_prices(price_rows)

    df = repo.get_prices_df()

    assert len(df) == len(price_rows)
    assert set(df["symbol"].unique()) == {"btc", "eth"}
    assert df["timestamp"].dtype.kind == "M"  # datetime64


def test_save_prices_ignores_empty_batch(clean_db):
    """An empty batch is a no-op rather than an error."""
    repo.save_prices([])
    assert repo.get_prices_df().empty


def test_since_filter_narrows_results(clean_db, price_rows):
    """The `since` argument filters in SQL and excludes older rows."""
    repo.save_prices(price_rows)

    cutoff = datetime.now() - timedelta(hours=12)
    recent = repo.get_prices_df(since=cutoff)
    everything = repo.get_prices_df()

    assert not recent.empty
    assert len(recent) < len(everything)
    # Timestamps are stored at second resolution, so a row right on the
    # boundary can sit up to a second before a cutoff carrying microseconds.
    assert recent["timestamp"].min() >= cutoff - timedelta(seconds=1)


def test_symbol_filter(clean_db, price_rows):
    """Filtering by symbol returns only that symbol."""
    repo.save_prices(price_rows)

    df = repo.get_prices_df("btc")

    assert not df.empty
    assert set(df["symbol"].unique()) == {"btc"}


def test_get_latest_prices_returns_newest_snapshot(clean_db, price_rows):
    """Only the rows sharing the most recent timestamp are returned."""
    repo.save_prices(price_rows)

    latest = repo.get_latest_prices()

    assert len(latest) == 2  # one row per symbol
    assert len({row["timestamp"] for row in latest}) == 1


def test_get_latest_prices_on_empty_database(clean_db):
    """An empty table yields an empty list, not an error."""
    assert repo.get_latest_prices() == []


def test_retention_deletes_only_old_rows(clean_db, price_rows):
    """Deleting before a cutoff leaves newer rows in place."""
    repo.save_prices(price_rows)
    before = len(repo.get_prices_df())

    cutoff = datetime.now() - timedelta(hours=36)
    removed = repo.delete_prices_before(cutoff)

    after = len(repo.get_prices_df())
    assert removed > 0
    assert after == before - removed
    # See the note in test_since_filter_narrows_results about second resolution.
    assert repo.get_prices_df()["timestamp"].min() >= cutoff - timedelta(seconds=1)


def test_sentiment_snapshot_round_trip(clean_db):
    """
    A snapshot's aggregates and its sources both come back.

    Sources are stored in their own table rather than inside the snapshot, so
    the reassembled result is not byte-identical to what went in: every source
    field is present, defaulting to None where the scrape supplied nothing.
    """
    payload = {
        "overall": {"sentiment": 63.5, "news_sentiment": 70, "fear_greed_index": 55},
        "sources": [
            {
                "type": "news",
                "title": "Example",
                "url": "https://example.com/a",
                "sentiment": 72,
                "mentioned_cryptos": ["BTC"],
            }
        ],
    }
    repo.save_sentiment_snapshot(payload)

    result = repo.get_latest_sentiment()

    assert result["overall"] == payload["overall"]
    assert len(result["sources"]) == 1
    assert result["sources"][0]["title"] == "Example"
    assert result["sources"][0]["mentioned_cryptos"] == ["BTC"]
    assert isinstance(repo.get_latest_sentiment_time(), datetime)


def test_sentiment_snapshots_since_are_oldest_first(clean_db):
    """Snapshots in a range come back in chronological order."""
    for value in (10, 20, 30):
        repo.save_sentiment_snapshot({"overall": {"sentiment": value}})

    snapshots = repo.get_sentiment_snapshots_since(datetime.now() - timedelta(days=1))

    assert len(snapshots) == 3
    timestamps = [created_at for created_at, _ in snapshots]
    assert timestamps == sorted(timestamps)


def test_top_gainers_round_trip(clean_db):
    """A top gainers snapshot returns its creation time and payload."""
    payload = [{"symbol": "sol", "name": "Solana", "percent_change_24h": 12.4}]
    repo.save_top_gainers(payload)

    created_at, data = repo.get_latest_top_gainers()

    assert isinstance(created_at, datetime)
    assert data == payload


def test_top_gainers_empty_database(clean_db):
    """No snapshot yields (None, [])."""
    assert repo.get_latest_top_gainers() == (None, [])
