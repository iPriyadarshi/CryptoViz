"""
Top gainers scraper.

Collects the cryptocurrencies with the largest 24-hour price increases.
CoinMarketCap's gainers/losers page is the primary source; when it fails the
CoinGecko markets API is filtered and sorted locally to produce the same shape.
"""

import logging
import re

from bs4 import BeautifulSoup

from .base import BaseScraper, extract_float

logger = logging.getLogger(__name__)


class TopGainersScraper(BaseScraper):
    """Scraper for the best-performing cryptocurrencies of the last 24 hours."""

    def __init__(self):
        """Initialize the top gainers scraper with its source URLs."""
        super().__init__()
        self.primary_url = "https://coinmarketcap.com/gainers-losers/"
        self.fallback_url = "https://api.coingecko.com/api/v3/coins/markets"

    def get_top_gainers(self, limit=20):
        """
        Get the top gaining cryptocurrencies of the last 24 hours.

        Args:
            limit (int, optional): Maximum number of gainers to return.

        Returns:
            list[dict]: One dict per cryptocurrency with keys symbol, name,
            price, market_cap, volume_24h, percent_change_24h, timestamp, rank.
            Empty when every source fails.
        """
        try:
            response = self.make_request(self.primary_url)
            if not response:
                return self.get_top_gainers_fallback(limit)

            soup = BeautifulSoup(response.text, "html.parser")

            # The gainers table is the first table on the page.
            gainers_table = soup.select_one("table.cmc-table")
            if not gainers_table:
                return self.get_top_gainers_fallback(limit)

            rows = gainers_table.select("tbody tr")[:limit]
            timestamp = self.get_current_timestamp()
            top_gainers = []

            for rank, row in enumerate(rows, 1):
                cols = row.find_all("td")
                if len(cols) < 7:
                    continue

                try:
                    name_tag = cols[2].find("p", class_=re.compile(r"sc-.*"))
                    symbol_tag = cols[2].find("p", class_="coin-item-symbol")

                    name = name_tag.get_text(strip=True) if name_tag else "unknown"
                    symbol = (
                        symbol_tag.get_text(strip=True).lower() if symbol_tag else "n/a"
                    )

                    top_gainers.append(
                        {
                            "symbol": symbol,
                            "name": name,
                            "price": extract_float(cols[3].text),
                            "market_cap": extract_float(cols[6].text),
                            "volume_24h": extract_float(cols[5].text),
                            "percent_change_24h": extract_float(cols[4].text),
                            "timestamp": timestamp,
                            "rank": rank,
                        }
                    )
                except AttributeError, IndexError, ValueError:
                    continue

            if top_gainers:
                return top_gainers

            logger.info("CoinMarketCap gainers page yielded no rows; using fallback")
            return self.get_top_gainers_fallback(limit)

        except Exception as exc:
            logger.warning("Top gainers scrape failed (%s); using fallback", exc)
            return self.get_top_gainers_fallback(limit)

    def get_top_gainers_fallback(self, limit=20):
        """
        Derive top gainers from the CoinGecko markets API.

        Pulls a wide slice of the market, keeps the entries with a positive 24h
        change and sorts them, since CoinGecko has no dedicated gainers endpoint.

        Args:
            limit (int, optional): Maximum number of gainers to return.

        Returns:
            list[dict]: Same shape as get_top_gainers(); empty on failure.
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

            response = self.make_request(self.fallback_url, params=params)
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
            logger.error("CoinGecko top gainers fallback failed: %s", exc)
            return []
