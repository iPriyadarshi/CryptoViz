"""
Tests for the scraping layer.

Network calls are stubbed: these verify the parsing and the fallback logic,
which are what actually break when an upstream site changes.
"""

from datetime import UTC, timedelta

import pytest

from cryptoviz.scrapers import CryptoDataScraper, TopGainersScraper, extract_float


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def test_extract_float_handles_formatted_numbers():
    """Currency symbols, thousands separators and signs are stripped."""
    assert extract_float("$1,234.56") == 1234.56
    assert extract_float("-5.7%") == -5.7
    assert extract_float("42") == 42.0


def test_extract_float_defaults_to_zero():
    """Unparseable input yields 0.0 rather than raising."""
    assert extract_float(None) == 0.0
    assert extract_float("no digits here") == 0.0
    assert extract_float({}) == 0.0


def test_extract_float_passes_numbers_through():
    """Values that are already numeric are returned as floats."""
    assert extract_float(7) == 7.0
    assert extract_float(7.5) == 7.5


def test_price_scraper_keeps_usd_amounts_and_signs(monkeypatch):
    """Rows carry raw USD, signed changes, and None for an unreported value."""
    scraper = CryptoDataScraper()

    payload = [
        {
            "symbol": "BTC",
            "name": "Bitcoin",
            "current_price": 65000,
            "market_cap": 1.2e12,
            "total_volume": 3.0e10,
            "price_change_percentage_24h": -2.1,
        },
        {
            "symbol": "NEW",
            "name": "New Coin",
            "current_price": 1.5,
            "market_cap": 1.0e9,
            "total_volume": 1.0e6,
            "price_change_percentage_24h": None,
        },
    ]

    monkeypatch.setattr(scraper, "make_request", lambda *a, **k: FakeResponse(payload))

    data = scraper.get_crypto_data()

    assert len(data) == 2
    assert data[0]["symbol"] == "btc"  # normalized to lowercase
    assert data[0]["price"] == 65000
    assert data[0]["market_cap"] == 1.2e12
    assert data[0]["percent_change_24h"] == -2.1
    assert data[1]["percent_change_24h"] is None
    assert "timestamp" in data[0]


def test_price_scraper_returns_empty_when_the_source_fails(monkeypatch):
    """Total failure yields an empty list, never an exception."""
    scraper = CryptoDataScraper()
    monkeypatch.setattr(scraper, "make_request", lambda *a, **k: None)

    assert scraper.get_crypto_data() == []


def test_top_gainers_filters_and_ranks(monkeypatch):
    """Only gainers are kept, sorted by change, and ranked."""
    scraper = TopGainersScraper()

    payload = [
        {
            "symbol": "AAA",
            "name": "A",
            "current_price": 1,
            "market_cap": 1,
            "total_volume": 1,
            "price_change_percentage_24h": 5.0,
        },
        {
            "symbol": "BBB",
            "name": "B",
            "current_price": 2,
            "market_cap": 2,
            "total_volume": 2,
            "price_change_percentage_24h": -3.0,
        },
        {
            "symbol": "CCC",
            "name": "C",
            "current_price": 3,
            "market_cap": 3,
            "total_volume": 3,
            "price_change_percentage_24h": 12.0,
        },
        {
            "symbol": "DDD",
            "name": "D",
            "current_price": 4,
            "market_cap": 4,
            "total_volume": 4,
            "price_change_percentage_24h": None,
        },
    ]

    monkeypatch.setattr(scraper, "make_request", lambda *a, **k: FakeResponse(payload))

    gainers = scraper.get_top_gainers(limit=10)

    # The loser and the null entry are dropped.
    assert [g["symbol"] for g in gainers] == ["ccc", "aaa"]
    assert [g["rank"] for g in gainers] == [1, 2]


def test_top_gainers_respects_limit(monkeypatch):
    """The limit caps the number of returned gainers."""
    scraper = TopGainersScraper()

    payload = [
        {
            "symbol": f"C{i}",
            "name": f"Coin {i}",
            "current_price": i,
            "market_cap": i,
            "total_volume": i,
            "price_change_percentage_24h": float(i),
        }
        for i in range(1, 30)
    ]
    monkeypatch.setattr(scraper, "make_request", lambda *a, **k: FakeResponse(payload))

    assert len(scraper.get_top_gainers(limit=5)) == 5


def test_make_request_swallows_network_errors():
    """An unreachable host yields None instead of propagating."""
    scraper = CryptoDataScraper()

    result = scraper.make_request(
        "http://127.0.0.1:1/definitely-not-listening", timeout=1
    )

    assert result is None


def _sentiment_scraper(news=(), reddit=(), fear_greed=None):
    """Build a sentiment scraper holding already-collected readings."""
    from cryptoviz.scrapers import SentimentScraper

    scraper = SentimentScraper()
    scraper.fear_greed_index = fear_greed
    scraper.sentiment_data["sources"] = [
        {"type": "news", "sentiment": score} for score in news
    ] + [{"type": "reddit", "sentiment": score} for score in reddit]
    return scraper


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Whether traders sold the automatic rally", []),
        ("BTCS shares climb on earnings", []),
        ("A ripple effect and a stellar quarter", []),
        ("BTC and ETH rally while SOL lags", ["BTC", "ETH", "SOL"]),
        ("bitcoin rebounds as Ripple wins in court", ["BTC", "XRP"]),
    ],
)
def test_mentions_match_whole_words(text, expected):
    """Substrings and everyday words are not counted as coin mentions."""
    from cryptoviz.scrapers import SentimentScraper

    assert SentimentScraper()._find_mentions(text) == expected


def test_rss_feed_parsing_uses_real_dates_and_dedupes(monkeypatch):
    """Publication dates are kept, old items dropped, repeats counted once."""
    from email.utils import format_datetime

    from cryptoviz.db.models import utc_now
    from cryptoviz.scrapers import SentimentScraper, sentiment

    recent = utc_now().replace(microsecond=0, tzinfo=UTC) - timedelta(hours=3)
    stale = recent - timedelta(days=30)
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Bitcoin rallies &amp; surges</title>
        <link>https://example.com/a</link>
        <description><![CDATA[<p>Great <b>news</b></p>]]></description>
        <pubDate>{format_datetime(recent)}</pubDate></item>
      <item><title>Old story</title><link>https://example.com/old</link>
        <pubDate>{format_datetime(stale)}</pubDate></item>
      <item><title>Bad link</title><link>javascript:alert(1)</link>
        <pubDate>{format_datetime(recent)}</pubDate></item>
    </channel></rss>""".encode()

    class Response:
        status_code = 200
        content = feed

    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: Response())

    scraper = SentimentScraper()
    items = scraper.scrape_crypto_news(limit=50)

    assert [item["url"] for item in items] == ["https://example.com/a"]
    assert items[0]["title"] == "Bitcoin rallies & surges"
    assert items[0]["text"] == "Great news"
    assert items[0]["timestamp"] == recent.strftime("%Y-%m-%d %H:%M:%S")
    assert len(scraper.sentiment_data["crypto_specific"]["BTC"]["sentiment_scores"]) == 1


def test_backfill_rows_keep_true_timestamps_and_usd():
    """Each upstream point becomes one row, with a real 24h change."""
    from cryptoviz.scrapers.historical import _build_rows

    start = 1_700_000_000_000
    hour = 3_600_000
    prices = [[start + i * hour, 100.0 + i] for i in range(30)]
    caps = [[ms, 1.0e12] for ms, _ in prices]
    volumes = [[ms, 3.0e10] for ms, _ in prices]

    rows = _build_rows("btc", "Bitcoin", prices, caps, volumes)

    assert len(rows) == 30
    assert len({row["timestamp"] for row in rows}) == 30
    assert rows[0]["market_cap"] == 1.0e12
    assert rows[0]["percent_change_24h"] is None
    assert rows[24]["percent_change_24h"] == pytest.approx(24.0)


def test_missing_source_is_renormalized_not_neutral():
    """With Reddit down, news and Fear & Greed keep their relative weights."""
    scraper = _sentiment_scraper(news=[40.0], fear_greed=11)

    overall = scraper.calculate_overall_sentiment()

    assert overall == pytest.approx((0.5 * 40 + 0.2 * 11) / 0.7)
    assert scraper.sentiment_data["overall"]["social_sentiment"] is None


def test_total_outage_keeps_the_last_good_snapshot(clean_db):
    """When every source fails, no fake neutral snapshot is stored."""
    from cryptoviz.db import repository as repo

    repo.save_sentiment_snapshot({"overall": {"sentiment": 27.2}})

    scraper = _sentiment_scraper()
    scraper.calculate_overall_sentiment()

    assert scraper.save_sentiment_data() is False
    assert repo.get_latest_sentiment()["overall"]["sentiment"] == 27.2
