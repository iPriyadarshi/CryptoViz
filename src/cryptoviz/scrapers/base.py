"""
Shared scraping infrastructure.

Every scraper in this package makes browser-like HTTP requests against a primary
HTML source and falls back to a JSON API when that fails, so the request
handling and the numeric parsing they all need live here.
"""

import logging
import re
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# Browser-like user agent; the crypto sites used as primary sources reject
# requests that advertise a scripting client.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


def extract_float(text):
    """
    Extract the first numeric value from a string.

    Handles thousands separators and leading minus signs, which is what the
    scraped price/percentage cells contain.

    Args:
        text: The value to parse. Numbers pass through; anything unparseable
            yields 0.0.

    Returns:
        float: The extracted value, or 0.0 when extraction fails.

    Example:
        >>> extract_float("$1,234.56")
        1234.56
        >>> extract_float("-5.7%")
        -5.7
    """
    if text is None:
        return 0.0

    if not isinstance(text, str):
        try:
            return float(text)
        except TypeError, ValueError:
            return 0.0

    try:
        match = re.search(r"-?[\d,.]+", text)
        if match:
            return float(match.group(0).replace(",", ""))
        return 0.0
    except TypeError, ValueError:
        return 0.0


class BaseScraper:
    """
    Base class for the data scrapers.

    Provides HTTP request handling with error suppression and a consistent
    timestamp format for the rows each scraper produces.
    """

    def __init__(self):
        """Initialize the scraper with browser-like request headers."""
        self.headers = {"User-Agent": DEFAULT_USER_AGENT}

    def get_current_timestamp(self):
        """Return the current time in the application's timestamp format."""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def make_request(self, url, params=None, timeout=15):
        """
        Make an HTTP GET request, returning None instead of raising on failure.

        Args:
            url (str): The URL to request.
            params (dict, optional): Query parameters.
            timeout (int, optional): Request timeout in seconds.

        Returns:
            requests.Response | None: The response when successful, else None.
        """
        try:
            response = requests.get(
                url, headers=self.headers, params=params, timeout=timeout
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            logger.warning("Request to %s failed: %s", url, exc)
            return None
