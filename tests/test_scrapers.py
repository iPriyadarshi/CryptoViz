"""
Tests for the scraping layer.

Network calls are stubbed: these verify the parsing and the fallback logic,
which are what actually break when an upstream site changes.
"""

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


def test_price_scraper_falls_back_when_primary_fails(monkeypatch):
    """A failed scrape is answered from the fallback API."""
    scraper = CryptoDataScraper()

    payload = [
        {
            "symbol": "BTC",
            "name": "Bitcoin",
            "current_price": 65000,
            "market_cap": 1.2e12,
            "total_volume": 3.0e10,
            "price_change_percentage_24h": 2.1,
        }
    ]

    def fake_request(url, params=None, timeout=15):
        # The primary source is unreachable; the fallback answers.
        return None if url == scraper.primary_url else FakeResponse(payload)

    monkeypatch.setattr(scraper, "make_request", fake_request)

    data = scraper.get_crypto_data()

    assert len(data) == 1
    assert data[0]["symbol"] == "btc"  # normalized to lowercase
    assert data[0]["price"] == 65000
    assert "timestamp" in data[0]


def test_price_scraper_returns_empty_when_every_source_fails(monkeypatch):
    """Total failure yields an empty list, never an exception."""
    scraper = CryptoDataScraper()
    monkeypatch.setattr(scraper, "make_request", lambda *a, **k: None)

    assert scraper.get_crypto_data() == []


def test_top_gainers_fallback_filters_and_ranks(monkeypatch):
    """The fallback keeps only gainers, sorted by change, and ranks them."""
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

    gainers = scraper.get_top_gainers_fallback(limit=10)

    # The loser and the null entry are dropped.
    assert [g["symbol"] for g in gainers] == ["ccc", "aaa"]
    assert [g["rank"] for g in gainers] == [1, 2]


def test_top_gainers_fallback_respects_limit(monkeypatch):
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

    assert len(scraper.get_top_gainers_fallback(limit=5)) == 5


def test_make_request_swallows_network_errors():
    """An unreachable host yields None instead of propagating."""
    scraper = CryptoDataScraper()

    result = scraper.make_request(
        "http://127.0.0.1:1/definitely-not-listening", timeout=1
    )

    assert result is None
