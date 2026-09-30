"""
Cryptocurrency price scraper.

Collects current market data for the top 10 cryptocurrencies by market cap.
CoinMarketCap's HTML listing is the primary source; the CoinGecko API is used
as a fallback whenever the scrape fails or yields nothing usable.
"""

import logging
import re

from bs4 import BeautifulSoup

from .base import BaseScraper, extract_float

logger = logging.getLogger(__name__)


class CryptoDataScraper(BaseScraper):
    """Scraper for top-10 cryptocurrency market data."""

    def __init__(self):
        """Initialize the price scraper with its source URLs."""
        super().__init__()
        self.primary_url = "https://coinmarketcap.com/"
        self.fallback_url = "https://api.coingecko.com/api/v3/coins/markets"

    def get_crypto_data(self):
        """
        Get current data for the top 10 cryptocurrencies by market cap.

        Scrapes CoinMarketCap for name, symbol, price, market cap, 24h volume
        and 24h percent change, falling back to the CoinGecko API when the
        scrape fails or produces no rows.

        Returns:
            list[dict]: One dict per cryptocurrency with keys symbol, name,
            price, market_cap, volume_24h, percent_change_24h, timestamp.
            Empty when every source fails.
        """
        try:
            response = self.make_request(self.primary_url)
            if not response:
                return self.get_crypto_data_fallback()

            soup = BeautifulSoup(response.text, "html.parser")
            rows = soup.select("table.cmc-table tbody tr")[:10]

            timestamp = self.get_current_timestamp()
            cryptos = []

            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 8:
                    continue  # Skip rows with insufficient columns

                try:
                    name_tag = cols[2].find("p", class_=re.compile(r"sc-.*"))
                    symbol_tag = cols[2].find("p", class_="coin-item-symbol")

                    name = name_tag.get_text(strip=True) if name_tag else "unknown"
                    symbol = (
                        symbol_tag.get_text(strip=True).lower() if symbol_tag else "n/a"
                    )

                    cryptos.append(
                        {
                            "symbol": symbol,
                            "name": name,
                            "price": extract_float(cols[3].text),
                            "market_cap": extract_float(cols[7].text),
                            "volume_24h": extract_float(cols[6].text),
                            "percent_change_24h": extract_float(cols[5].text),
                            "timestamp": timestamp,
                        }
                    )
                except AttributeError, IndexError, ValueError:
                    continue

            if cryptos:
                return cryptos

            logger.info("CoinMarketCap returned no usable rows; using fallback")
            return self.get_crypto_data_fallback()

        except Exception as exc:
            logger.warning("Price scrape failed (%s); using fallback", exc)
            return self.get_crypto_data_fallback()

    def get_crypto_data_fallback(self):
        """
        Get top-10 cryptocurrency data from the CoinGecko API.

        Returns:
            list[dict]: Same shape as get_crypto_data(); empty on failure.
        """
        try:
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
                "sparkline": False,
            }

            response = self.make_request(self.fallback_url, params=params)
            if not response:
                return []

            timestamp = self.get_current_timestamp()

            return [
                {
                    "symbol": crypto.get("symbol", "").lower(),
                    "name": crypto.get("name", ""),
                    "price": crypto.get("current_price", 0),
                    "market_cap": crypto.get("market_cap", 0),
                    "volume_24h": crypto.get("total_volume", 0),
                    "percent_change_24h": crypto.get("price_change_percentage_24h", 0),
                    "timestamp": timestamp,
                }
                for crypto in response.json()
            ]

        except Exception as exc:
            logger.error("CoinGecko price fallback failed: %s", exc)
            return []
