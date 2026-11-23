"""
Cryptocurrency price scraper.

Collects current market data for the top 10 cryptocurrencies by market cap from
the CoinGecko API, which reports signed changes and raw USD amounts. A failed
request stores nothing, so a gap in the series is never filled with bad values.
"""

import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)


class CryptoDataScraper(BaseScraper):
    """Scraper for top-10 cryptocurrency market data."""

    def __init__(self):
        """Initialize the price scraper with its source URL."""
        super().__init__()
        self.source_url = "https://api.coingecko.com/api/v3/coins/markets"

    def get_crypto_data(self):
        """
        Get current data for the top 10 cryptocurrencies by market cap.

        Returns:
            list[dict]: One dict per cryptocurrency with keys symbol, name,
            price, market_cap, volume_24h, percent_change_24h, timestamp.
            market_cap and volume_24h are in USD. A value the source does not
            report is None. Empty when the request fails.
        """
        try:
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
                "sparkline": False,
            }

            response = self.make_request(self.source_url, params=params)
            if not response:
                return []

            timestamp = self.get_current_timestamp()

            return [
                {
                    "symbol": crypto.get("symbol", "").lower(),
                    "name": crypto.get("name", ""),
                    "price": crypto.get("current_price"),
                    "market_cap": crypto.get("market_cap"),
                    "volume_24h": crypto.get("total_volume"),
                    "percent_change_24h": crypto.get("price_change_percentage_24h"),
                    "timestamp": timestamp,
                }
                for crypto in response.json()
            ]

        except Exception as exc:
            logger.error("CoinGecko price request failed: %s", exc)
            return []
