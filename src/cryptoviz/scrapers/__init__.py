"""
Data collection package.

Each module wraps one external data source behind a class that returns plain
dicts, so the services layer never deals with HTML, APIs or rate limits.
"""

from .base import BaseScraper, extract_float
from .prices import CryptoDataScraper
from .sentiment import SentimentScraper
from .top_gainers import TopGainersScraper

__all__ = [
    "BaseScraper",
    "CryptoDataScraper",
    "SentimentScraper",
    "TopGainersScraper",
    "extract_float",
]
