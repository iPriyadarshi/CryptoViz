"""
Tests for the HTTP layer.

These check status codes and response shape - the contract the frontend relies
on - rather than the numbers, which the analytics tests cover.
"""

import pytest

from cryptoviz.api.pages import FRONTEND_PAGES


def test_healthz(client):
    """The health check answers without touching the database."""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


@pytest.mark.parametrize("page", FRONTEND_PAGES)
def test_every_page_renders(client, page):
    """Each registered frontend page returns HTML."""
    response = client.get(f"/{page}.html")

    assert response.status_code == 200
    assert b"<!DOCTYPE html>" in response.data


def test_home_serves_the_dashboard(client):
    """The site root serves the dashboard."""
    response = client.get("/")

    assert response.status_code == 200
    assert b"CryptoViz" in response.data


def test_api_docs_page(client):
    """The API documentation page renders."""
    assert client.get("/api-docs").status_code == 200


def test_static_assets_are_served_at_the_site_root(client):
    """Frontend references like /js/config.js resolve."""
    assert client.get("/js/config.js").status_code == 200
    assert client.get("/css/theme.css").status_code == 200


def test_crypto_endpoint_shape(client, clean_db, price_rows):
    """The latest-data endpoint wraps its rows in a 'data' key."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    body = client.get("/api/crypto").get_json()

    assert "data" in body
    assert len(body["data"]) == 2
    assert {"symbol", "name", "price", "timestamp"} <= set(body["data"][0])


def test_history_endpoint_shape(client, clean_db, price_rows):
    """History returns parallel price and timestamp arrays."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    body = client.get("/api/crypto/btc/history").get_json()

    assert len(body["prices"]) == len(body["timestamps"])


def test_volatility_endpoints(client, clean_db, price_rows):
    """Both the per-symbol and all-symbol volatility endpoints respond."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    single = client.get("/api/crypto/btc/volatility?days=7")
    assert single.status_code == 200
    assert "volatility" in single.get_json()

    everything = client.get("/api/crypto/volatility?days=7")
    assert everything.status_code == 200
    assert len(everything.get_json()["data"]) == 2


@pytest.mark.parametrize("days", ["0", "-5", "abc", ""])
def test_invalid_days_parameter_falls_back(client, clean_db, price_rows, days):
    """A nonsensical `days` value uses the default instead of erroring."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    response = client.get(f"/api/crypto/volatility?days={days}")

    assert response.status_code == 200
    assert "data" in response.get_json()


def test_correlation_endpoint(client, clean_db, price_rows):
    """Correlation returns a matrix and its symbol labels."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)

    body = client.get("/api/crypto/correlation?days=7").get_json()

    assert "correlation_matrix" in body
    assert "symbols" in body
    assert len(body["correlation_matrix"]) == len(body["symbols"])


def test_sentiment_returns_404_when_empty(client, clean_db):
    """With no snapshots the sentiment endpoints report 404, not 500."""
    assert client.get("/api/sentiment").status_code == 404
    assert client.get("/api/sentiment/overall").status_code == 404
    assert client.get("/api/sentiment/rankings").status_code == 404


def test_sentiment_endpoints_with_data(client, clean_db):
    """A stored snapshot is served through the sentiment endpoints."""
    from cryptoviz.db import repository as repo

    repo.save_sentiment_snapshot(
        {
            "overall": {
                "sentiment": 62.0,
                "social_sentiment": 58.0,
                "news_sentiment": 65.0,
                "fear_greed_index": 60,
            },
            "rankings": {"positive": [{"symbol": "BTC", "score": 71}], "negative": []},
            "sources": [
                {
                    "type": "news",
                    "title": "Example",
                    "url": "https://example.com/a",
                    "sentiment": 71,
                    "mentioned_cryptos": ["BTC"],
                    "timestamp": "2026-01-01 00:00:00",
                }
            ],
        }
    )

    overall = client.get("/api/sentiment/overall").get_json()
    assert overall["sentiment"] == 62.0

    rankings = client.get("/api/sentiment/rankings").get_json()
    assert rankings["positive"][0]["symbol"] == "BTC"

    sources = client.get("/api/sentiment/sources?type=news").get_json()
    assert sources["count"] == 1

    filtered = client.get("/api/sentiment/sources?crypto=ETH").get_json()
    assert filtered["count"] == 0


def test_sentiment_trends_shape(client, clean_db, price_rows):
    """Trends returns three equal-length parallel arrays."""
    from cryptoviz.db import repository as repo

    repo.save_prices(price_rows)
    repo.save_sentiment_snapshot(
        {
            "overall": {"sentiment": 70.0},
            "crypto_specific": {"BTC": {"sentiment_scores": [10.0, 20.0]}},
        }
    )

    body = client.get("/api/sentiment/trends?days=7&symbol=BTC").get_json()

    assert len(body["dates"]) == len(body["sentiment"]) == len(body["price"])
    assert len(body["dates"]) == 1
    # A coin's trend uses that coin's scores, not the whole-market reading.
    assert body["sentiment"] == [15.0]
    # The price was matched against the stored series, not left null.
    assert body["price"][0] is not None

    market = client.get("/api/sentiment/trends?days=7&symbol=all").get_json()
    assert market["sentiment"] == [70.0]


def test_sentiment_trends_leave_a_gap_without_a_reading(client, clean_db):
    """A coin no article mentioned is not plotted as a neutral 50."""
    from cryptoviz.db import repository as repo

    repo.save_sentiment_snapshot({"overall": {"sentiment": 55.0}})

    body = client.get("/api/sentiment/trends?days=7&symbol=ETH").get_json()

    assert body == {"dates": [], "sentiment": [], "price": []}


def test_sentiment_trends_empty_without_snapshots(client, clean_db):
    """No snapshots means empty arrays, not a 500."""
    body = client.get("/api/sentiment/trends?days=7").get_json()

    assert body == {"dates": [], "sentiment": [], "price": []}


def test_top_gainers_with_data(client, clean_db):
    """A stored snapshot is served with its collection time."""
    from cryptoviz.db import repository as repo

    repo.save_top_gainers(
        [{"symbol": "sol", "name": "Solana", "percent_change_24h": 12.4, "rank": 1}]
    )

    body = client.get("/api/top-gainers").get_json()

    assert len(body["data"]) == 1
    assert body["last_updated"] is not None


def test_top_gainers_respects_limit(client, clean_db):
    """The limit parameter truncates the stored snapshot."""
    from cryptoviz.db import repository as repo

    repo.save_top_gainers(
        [{"symbol": f"c{i}", "name": f"Coin {i}", "rank": i} for i in range(1, 31)]
    )

    body = client.get("/api/top-gainers?limit=5").get_json()

    assert len(body["data"]) == 5


@pytest.mark.parametrize(
    "path", ["/api/top-gainers/update?force=true", "/api/sentiment/update?force=true"]
)
def test_requests_cannot_trigger_a_scrape(client, path):
    """Scraping belongs to the worker, so the old trigger routes are gone."""
    assert client.get(path).status_code == 404


def test_top_gainers_read_never_scrapes_or_writes(client, clean_db, monkeypatch):
    """An empty table is a 404, and a reader's limit cannot shrink stored data."""
    from cryptoviz.db import repository as repo
    from cryptoviz.scrapers import TopGainersScraper

    def fail(*_args, **_kwargs):
        raise AssertionError("a GET must not scrape")

    monkeypatch.setattr(TopGainersScraper, "get_top_gainers", fail)

    assert client.get("/api/top-gainers?limit=1").status_code == 404
    assert repo.get_latest_top_gainers() == (None, [])


def test_sources_limit_is_capped(client, clean_db):
    """An absurd limit is clamped instead of overflowing SQLite."""
    response = client.get("/api/sentiment/sources?limit=9223372036854775808")

    assert response.status_code == 200


def test_overall_ignores_missing_components(client, clean_db):
    """A source that was unavailable stays null instead of averaging as 50."""
    from cryptoviz.db import repository as repo

    for news in (20.0, 40.0):
        repo.save_sentiment_snapshot(
            {
                "overall": {
                    "sentiment": news,
                    "social_sentiment": None,
                    "news_sentiment": news,
                    "fear_greed_index": None,
                }
            }
        )

    body = client.get("/api/sentiment/overall?days=7").get_json()

    assert body["news_sentiment"] == 30.0
    assert body["social_sentiment"] is None


def test_overall_with_no_data_is_404_for_every_window(client, clean_db):
    """No window invents a neutral reading on an empty database."""
    assert client.get("/api/sentiment/overall?days=7").status_code == 404
