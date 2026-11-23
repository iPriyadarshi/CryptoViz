"""
Top gainers scraper.

Collects the cryptocurrencies with the largest 24-hour price increases. CoinGecko
has no dedicated gainers endpoint, so its markets API is filtered and sorted
locally.
"""

import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)


class TopGainersScraper(BaseScraper):
    """Scraper for the best-performing cryptocurrencies of the last 24 hours."""

    def __init__(self):
        """Initialize the top gainers scraper with its source URL."""
        super().__init__()
        self.source_url = "https://api.coingecko.com/api/v3/coins/markets"

    def get_top_gainers(self, limit=20):
        """
        Get the top gaining cryptocurrencies of the last 24 hours.

        Pulls a wide slice of the market, keeps the entries with a positive 24h
        change and sorts them.

        Args:
            limit (int, optional): Maximum number of gainers to return.

        Returns:
            list[dict]: One dict per cryptocurrency with keys symbol, name,
            price, market_cap, volume_24h, percent_change_24h, timestamp, rank.
            Empty when the request fails.
        """
        try:
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,  # Wide slice to filter for gainers
                "page": 1,
                "sparkline": False,
                "price_change_percentage": "24h",
            }

            response = self.make_request(self.source_url, params=params)
            if not response:
                return []

            timestamp = self.get_current_timestamp()

            # Keep only cryptocurrencies that actually gained.
            gainers = [
                crypto
                for crypto in response.json()
                if crypto.get("price_change_percentage_24h") is not None
                and float(crypto["price_change_percentage_24h"]) > 0
            ]

            gainers.sort(
                key=lambda c: float(c.get("price_change_percentage_24h") or 0),
                reverse=True,
            )
            gainers = gainers[:limit]

            def as_float(value):
                """Coerce a possibly-missing API value to a float."""
                return float(value) if value is not None else 0.0

            return [
                {
                    "symbol": crypto.get("symbol", "").lower(),
                    "name": crypto.get("name", ""),
                    "price": as_float(crypto.get("current_price")),
                    "market_cap": as_float(crypto.get("market_cap")),
                    "volume_24h": as_float(crypto.get("total_volume")),
                    "percent_change_24h": as_float(
                        crypto.get("price_change_percentage_24h")
                    ),
                    "timestamp": timestamp,
                    "rank": rank,
                }
                for rank, crypto in enumerate(gainers, 1)
            ]

        except Exception as exc:
            logger.error("CoinGecko top gainers request failed: %s", exc)
            return []
