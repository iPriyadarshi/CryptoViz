"""
Tests for normalized sentiment source storage.

The point of the normalization is that a source seen in many scrapes occupies
one row, and that filtering happens in SQL. These tests pin both.
"""

from datetime import datetime, timedelta

from cryptoviz.db import repository as repo


def _snapshot(sources, sentiment=60.0):
    """Build a scrape result carrying the given sources."""
    return {
        "overall": {"sentiment": sentiment},
        "crypto_specific": {},
        "rankings": {"positive": [], "negative": []},
        "sources": sources,
    }


def _news(title, url, cryptos=(), sentiment=70.0):
    """Build one news source dict as the scraper produces it."""
    return {
        "type": "news",
        "source": "CoinDesk",
        "title": title,
        "url": url,
        "sentiment": sentiment,
        "timestamp": "2026-01-01 12:00:00",
        "mentioned_cryptos": list(cryptos),
    }


def test_repeated_source_is_stored_once(clean_db):
    """
    The same article across many scrapes yields one row.

    This is the whole reason sources were pulled out of the snapshot blob: a
    year of 5-minute scrapes would otherwise store the same article ~100,000
    times.
    """
    article = _news("Bitcoin rallies", "https://example.com/btc")

    for _ in range(25):
        repo.save_sentiment_snapshot(_snapshot([article]))

    sources = repo.query_sentiment_sources()

    assert len(sources) == 1
    assert repo.count_sentiment_sources() == 1


def test_snapshot_blob_excludes_sources(clean_db):
    """
    The stored snapshot holds aggregates only.

    Keeping sources out of the blob is what bounds snapshot growth.
    """
    repo.save_sentiment_snapshot(_snapshot([_news("A", "https://example.com/a")]))

    snapshots = repo.get_sentiment_snapshots_since(datetime.now() - timedelta(days=1))

    assert len(snapshots) == 1
    _created_at, data = snapshots[0]
    assert "sources" not in data
    assert data["overall"]["sentiment"] == 60.0


def test_crypto_specific_keeps_scores_but_drops_sources(clean_db):
    """Per-crypto scores survive; their duplicated source lists do not."""
    payload = _snapshot([])
    payload["crypto_specific"] = {
        "BTC": {
            "sentiment_scores": [70.0, 80.0],
            "sources": [{"type": "news", "title": "A", "sentiment": 70}],
        }
    }
    repo.save_sentiment_snapshot(payload)

    _created_at, data = repo.get_sentiment_snapshots_since(
        datetime.now() - timedelta(days=1)
    )[0]

    assert data["crypto_specific"]["BTC"]["sentiment_scores"] == [70.0, 80.0]
    assert "sources" not in data["crypto_specific"]["BTC"]
    assert data["crypto_specific"]["BTC"]["sources_count"] == 1


def test_filter_by_type(clean_db):
    """Type filtering happens in SQL and returns only that type."""
    reddit = {
        "type": "reddit",
        "source": "r/CryptoCurrency",
        "title": "Discussion",
        "url": "https://reddit.com/x",
        "sentiment": 55.0,
        "timestamp": "2026-01-01 10:00:00",
        "mentioned_cryptos": ["ETH"],
    }
    repo.save_sentiment_snapshot(
        _snapshot([_news("News item", "https://example.com/n"), reddit])
    )

    news_only = repo.query_sentiment_sources(source_type="news")
    reddit_only = repo.query_sentiment_sources(source_type="reddit")

    assert len(news_only) == 1
    assert news_only[0]["type"] == "news"
    assert len(reddit_only) == 1
    assert reddit_only[0]["type"] == "reddit"


def test_filter_by_crypto(clean_db):
    """Crypto filtering matches through the mentions table."""
    repo.save_sentiment_snapshot(
        _snapshot(
            [
                _news("BTC piece", "https://example.com/1", cryptos=["BTC"]),
                _news("ETH piece", "https://example.com/2", cryptos=["ETH"]),
                _news("Both", "https://example.com/3", cryptos=["BTC", "ETH"]),
            ]
        )
    )

    btc = repo.query_sentiment_sources(crypto="BTC")
    eth = repo.query_sentiment_sources(crypto="ETH")
    sol = repo.query_sentiment_sources(crypto="SOL")

    assert len(btc) == 2
    assert len(eth) == 2
    assert sol == []


def test_crypto_filter_is_case_insensitive(clean_db):
    """A lowercase symbol matches the stored uppercase mention."""
    repo.save_sentiment_snapshot(
        _snapshot([_news("BTC piece", "https://example.com/1", cryptos=["BTC"])])
    )

    assert len(repo.query_sentiment_sources(crypto="btc")) == 1


def test_limit_applies(clean_db):
    """The limit is applied in SQL."""
    repo.save_sentiment_snapshot(
        _snapshot([_news(f"Item {i}", f"https://example.com/{i}") for i in range(20)])
    )

    assert len(repo.query_sentiment_sources(limit=5)) == 5
    # The unlimited count still reports everything stored.
    assert repo.count_sentiment_sources() == 20


def test_sources_without_url_dedupe_on_title(clean_db):
    """A source with no link still de-duplicates rather than piling up."""
    untitled = {
        "type": "news",
        "source": "Unknown",
        "title": "No link here",
        "url": "",
        "sentiment": 50.0,
        "timestamp": "2026-01-01 09:00:00",
        "mentioned_cryptos": [],
    }

    for _ in range(5):
        repo.save_sentiment_snapshot(_snapshot([untitled]))

    assert repo.count_sentiment_sources() == 1


def test_retention_removes_stale_sources_and_mentions(clean_db):
    """Cleanup drops old sources along with their mention rows."""
    repo.save_sentiment_snapshot(
        _snapshot([_news("Old", "https://example.com/old", cryptos=["BTC"])])
    )
    assert repo.count_sentiment_sources() == 1

    # Everything just written is older than a cutoff in the future.
    repo.delete_sentiment_before(datetime.now() + timedelta(days=1))

    assert repo.count_sentiment_sources() == 0
    # The mention rows went with it, so a crypto filter finds nothing.
    assert repo.query_sentiment_sources(crypto="BTC") == []


def test_since_filter_excludes_unseen_sources(clean_db):
    """A source not seen inside the window is excluded."""
    repo.save_sentiment_snapshot(_snapshot([_news("A", "https://example.com/a")]))

    future_cutoff = datetime.now() + timedelta(days=1)
    past_cutoff = datetime.now() - timedelta(days=1)

    assert repo.query_sentiment_sources(since=past_cutoff)
    assert repo.query_sentiment_sources(since=future_cutoff) == []
