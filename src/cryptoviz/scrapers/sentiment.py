"""
Cryptocurrency Market Sentiment Scraper Module

This module provides functionality to scrape and analyze sentiment data from various
sources including cryptocurrency news websites, Reddit, and other social media platforms.
It uses natural language processing to determine sentiment scores for the overall market
and specific cryptocurrencies.

The module collects data from multiple sources, processes it to extract sentiment,
and aggregates it into a comprehensive sentiment analysis report.
"""

import html
import logging
import re
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import cache

import nltk
import requests
from bs4 import BeautifulSoup
from nltk.sentiment.vader import SentimentIntensityAnalyzer

from .. import config
from ..db import repository as db
from ..db.models import utc_now
from .base import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)


def _ensure_vader_lexicon():
    """
    Make sure the VADER lexicon is available, downloading it only if missing.

    The resource has to be looked up by its real path ("sentiment/vader_lexicon"),
    not by bare name - a bare name never resolves, so the check would report the
    lexicon missing and re-download it on every process start.
    """
    try:
        nltk.data.find("sentiment/vader_lexicon.zip")
        return
    except LookupError:
        pass

    try:
        nltk.data.find("sentiment/vader_lexicon")
        return
    except LookupError:
        logger.info("VADER lexicon not found locally; downloading it")
        nltk.download("vader_lexicon", quiet=True)


@cache
def _get_analyzer():
    """
    Return the VADER analyzer, loading the lexicon on first use.

    Loaded here rather than at import, so a missing lexicon only affects
    sentiment scoring instead of stopping the worker from collecting prices. A
    failed load is not cached and is retried on the next scrape.
    """
    _ensure_vader_lexicon()
    return SentimentIntensityAnalyzer()


# RSS feeds carry stable markup and real publication dates, unlike the sites'
# front pages, whose HTML changes without notice.
NEWS_FEEDS = (
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("CoinDesk", "https://coindesk.com/arc/outboundfeeds/rss/"),
    ("Bitcoin.com", "https://news.bitcoin.com/feed/"),
    ("CryptoNews", "https://cryptonews.com/news/feed/"),
    ("Bitcoinist", "https://bitcoinist.com/feed/"),
)

# Older articles say little about the current market.
MAX_ARTICLE_AGE_DAYS = 7

MAX_FEED_BYTES = 5 * 1024 * 1024

# Coin names that are also ordinary words. These match only when capitalized,
# so "a ripple effect" is not read as XRP.
AMBIGUOUS_COIN_NAMES = {"Avalanche", "Cosmos", "Polygon", "Ripple", "Stellar"}


class SentimentScraper:
    """
    A class for scraping and analyzing cryptocurrency market sentiment from various sources.

    This class provides methods to collect sentiment data from news websites, Reddit,
    and other sources, analyze the sentiment using natural language processing,
    and aggregate the results into a comprehensive sentiment report.
    """

    def __init__(self):
        """
        Initialize the SentimentScraper with necessary configuration.

        Sets up HTTP headers for web requests, defines the list of cryptocurrencies to track,
        and initializes data structures for storing sentiment information.
        """
        # Browser-like user agent to avoid being blocked by websites
        self.headers = {"User-Agent": DEFAULT_USER_AGENT}

        # List of cryptocurrency symbols to track
        self.crypto_symbols = [
            "BTC",
            "ETH",
            "BNB",
            "SOL",
            "XRP",
            "ADA",
            "DOGE",
            "SHIB",
            "DOT",
            "AVAX",
            "MATIC",
            "LTC",
            "LINK",
            "UNI",
            "ATOM",
            "XLM",
            "ALGO",
            "FIL",
            "HBAR",
            "VET",
        ]

        # Mapping of cryptocurrency symbols to their full names
        self.crypto_names = {
            "BTC": "Bitcoin",
            "ETH": "Ethereum",
            "BNB": "Binance Coin",
            "SOL": "Solana",
            "XRP": "Ripple",
            "ADA": "Cardano",
            "DOGE": "Dogecoin",
            "SHIB": "Shiba Inu",
            "DOT": "Polkadot",
            "AVAX": "Avalanche",
            "MATIC": "Polygon",
            "LTC": "Litecoin",
            "LINK": "Chainlink",
            "UNI": "Uniswap",
            "ATOM": "Cosmos",
            "XLM": "Stellar",
            "ALGO": "Algorand",
            "FIL": "Filecoin",
            "HBAR": "Hedera",
            "VET": "VeChain",
        }

        # Tickers match as whole, uppercase words, so "whether" is not ETH and
        # "sold" is not SOL.
        self.mention_patterns = {
            symbol: re.compile(
                rf"\b{symbol}\b|\b{re.escape(name)}\b"
                if name in AMBIGUOUS_COIN_NAMES
                else rf"\b{symbol}\b|(?i:\b{re.escape(name)}\b)"
            )
            for symbol, name in self.crypto_names.items()
        }

        # Initialize data structure for storing sentiment information
        self.sentiment_data = {
            "overall": {},  # Overall market sentiment metrics
            "crypto_specific": {},  # Sentiment data for specific cryptocurrencies
            "sources": [],  # Raw sources used for sentiment analysis
        }

        # Initialize Fear & Greed Index (will be populated during scraping)
        self.fear_greed_index = None

        # Whether this run stored a snapshot
        self.saved = False

    def scrape_fear_and_greed_index(self):
        """
        Scrape the Crypto Fear & Greed Index from alternative.me.

        The Fear & Greed Index is a market sentiment indicator that measures
        investor sentiment on a scale from 0 (extreme fear) to 100 (extreme greed).
        It's based on various market factors including volatility, market momentum,
        social media, and surveys.

        Returns:
            int | None: The current Fear & Greed Index value (0-100), or None if scraping fails
        """
        try:
            # URL for the Fear & Greed Index
            url = "https://alternative.me/crypto/fear-and-greed-index/"

            # Make the HTTP request
            response = requests.get(url, headers=self.headers, timeout=15)

            # Parse the HTML content
            soup = BeautifulSoup(response.text, "html.parser")

            # Find the fear and greed value (typically in a div with class 'fng-circle')
            fng_value = soup.find("div", class_="fng-circle")
            if fng_value:
                # Extract the numeric value
                value = fng_value.text.strip()
                try:
                    # Convert to integer
                    self.fear_greed_index = int(value)
                    return self.fear_greed_index
                except ValueError:
                    pass

            return None  # Element not found
        except Exception:
            return None

    def scrape_crypto_news(self, limit=50):
        """
        Collect cryptocurrency news from the RSS feeds.

        The limit is shared evenly so that no single publication dominates the
        score, and an article carried by two feeds is counted once.

        Args:
            limit (int, optional): Maximum number of news articles to collect.

        Returns:
            list: A list of dictionaries containing news article data, including title,
                  summary, source, URL, sentiment score, and mentioned cryptocurrencies
        """
        news_items = []
        seen_urls = set()
        per_feed = max(1, limit // len(NEWS_FEEDS))

        for name, url in NEWS_FEEDS:
            try:
                response = requests.get(url, headers=self.headers, timeout=10)
                if response.status_code != 200:
                    logger.warning("%s feed returned HTTP %s", name, response.status_code)
                    continue
                if len(response.content) > MAX_FEED_BYTES:
                    logger.warning("%s feed is too large; skipping it", name)
                    continue

                for entry in self._parse_feed(response.content)[:per_feed]:
                    if entry["url"] in seen_urls:
                        continue
                    seen_urls.add(entry["url"])
                    news_items.append(self._build_source("news", name, **entry))
            except Exception as exc:
                logger.warning("Could not read the %s feed: %s", name, exc)

        self.sentiment_data["sources"].extend(news_items)
        return news_items

    def _parse_feed(self, content):
        """
        Parse an RSS document into recent articles.

        Args:
            content (bytes): The raw feed.

        Returns:
            list[dict]: Entries with title, summary, url and timestamp.
        """
        oldest = utc_now() - timedelta(days=MAX_ARTICLE_AGE_DAYS)
        entries = []

        for item in ElementTree.fromstring(content).iter("item"):
            title = html.unescape(item.findtext("title") or "").strip()
            url = (item.findtext("link") or "").strip()
            if not title or not url.startswith(("http://", "https://")):
                continue

            published = self._parse_published(item.findtext("pubDate"))
            if published < oldest:
                continue

            summary = BeautifulSoup(
                item.findtext("description") or "", "html.parser"
            ).get_text(" ", strip=True)

            entries.append(
                {
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "timestamp": published.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

        return entries

    @staticmethod
    def _parse_published(value):
        """Parse an RSS date to naive UTC, using the current time if it is unusable."""
        now = utc_now()
        try:
            published = parsedate_to_datetime(value)
        except TypeError, ValueError:
            return now

        if published.tzinfo is not None:
            published = published.astimezone(UTC).replace(tzinfo=None)
        return min(published, now)

    def _find_mentions(self, text):
        """Return the tracked symbols a piece of text actually mentions."""
        return [
            symbol
            for symbol, pattern in self.mention_patterns.items()
            if pattern.search(text)
        ]

    def _build_source(self, source_type, source_name, title, summary, url, timestamp):
        """
        Score one article or post and record it against the coins it mentions.

        Returns:
            dict: The source entry.
        """
        text_to_analyze = f"{title} {summary}"
        compound = _get_analyzer().polarity_scores(text_to_analyze)["compound"]
        normalized_score = self.normalize_sentiment_score(compound)
        mentioned_cryptos = self._find_mentions(text_to_analyze)

        for crypto in mentioned_cryptos:
            entry = self.sentiment_data["crypto_specific"].setdefault(
                crypto, {"sentiment_scores": [], "sources": []}
            )
            entry["sentiment_scores"].append(normalized_score)
            entry["sources"].append(
                {"type": source_type, "title": title, "sentiment": normalized_score}
            )

        return {
            "type": source_type,
            "source": source_name,
            "title": title,
            "text": summary[:200] + "..." if len(summary) > 200 else summary,
            "url": url,
            "timestamp": timestamp,
            "sentiment": normalized_score,
            "mentioned_cryptos": mentioned_cryptos,
        }

    def normalize_sentiment_score(self, score, min_val=-1, max_val=1):
        """
        Convert a sentiment score from the VADER scale (-1 to 1) to a 0-100 scale.

        VADER sentiment scores range from -1 (extremely negative) to 1 (extremely positive).
        This method converts that range to a more intuitive 0-100 scale where:
        - 0 represents extremely negative sentiment
        - 50 represents neutral sentiment
        - 100 represents extremely positive sentiment

        Args:
            score (float): The original sentiment score (typically between -1 and 1)
            min_val (float, optional): The minimum value of the original scale. Defaults to -1.
            max_val (float, optional): The maximum value of the original scale. Defaults to 1.

        Returns:
            float: The normalized sentiment score on a 0-100 scale
        """
        return ((score - min_val) / (max_val - min_val)) * 100

    def scrape_reddit_sentiment(self, subreddit="CryptoCurrency", limit=10):
        """
        Scrape sentiment data from Reddit cryptocurrency communities.

        This method collects posts from specified subreddits, extracts their content,
        analyzes sentiment, and identifies which cryptocurrencies are mentioned.
        Reddit is a valuable source of retail investor sentiment in the crypto market.

        Args:
            subreddit (str, optional): The subreddit to scrape. Defaults to 'CryptoCurrency'.
            limit (int, optional): Maximum number of posts to collect. Defaults to 10.

        Returns:
            list: A list of dictionaries containing Reddit post data, including title,
                  content, URL, sentiment score, and mentioned cryptocurrencies
        """
        reddit_items = []
        try:
            # Using old.reddit.com which has a simpler HTML structure for scraping
            url = f"https://old.reddit.com/r/{subreddit}/hot/"

            # Make the HTTP request
            response = requests.get(url, headers=self.headers, timeout=15)
            if response.status_code != 200:
                logger.warning("Reddit returned HTTP %s", response.status_code)
                return []

            # Parse the HTML content
            soup = BeautifulSoup(response.text, "html.parser")

            # Find post elements (each post is in a div with class 'thing')
            posts = soup.find_all("div", class_="thing", limit=limit)

            # Process each post
            for post in posts:
                try:
                    # Extract post title
                    title_element = post.find("a", class_="title")
                    if not title_element:
                        continue  # Skip posts without a title

                    title = title_element.text.strip()

                    # Extract post link
                    link = title_element["href"]
                    # Convert relative URLs to absolute URLs
                    if link and not link.startswith("http"):
                        link = "https://www.reddit.com" + link

                    # Extract post content if available (only for text posts)
                    content = ""
                    if "self" in post.get("class", []):  # Check if it's a text post
                        expando = post.find("div", class_="expando")
                        if expando:
                            content_element = expando.find("div", class_="md")
                            if content_element:
                                content = content_element.text.strip()

                    reddit_items.append(
                        self._build_source(
                            "reddit",
                            f"r/{subreddit}",
                            title=title,
                            summary=content,
                            url=link,
                            timestamp=self._post_time(post),
                        )
                    )

                except Exception:
                    continue  # Skip this post and continue with the next one

            # Add all Reddit items to the main sources list
            self.sentiment_data["sources"].extend(reddit_items)
            return reddit_items

        except Exception as exc:
            logger.warning("Could not read Reddit: %s", exc)
            return []  # Return empty list on error

    @staticmethod
    def _post_time(post):
        """Return a Reddit post's creation time, or now if it is not available."""
        try:
            created = datetime.fromtimestamp(int(post["data-timestamp"]) / 1000, UTC)
            moment = created.replace(tzinfo=None)
        except KeyError, TypeError, ValueError, OverflowError, OSError:
            moment = utc_now()
        return moment.strftime("%Y-%m-%d %H:%M:%S")

    def calculate_overall_sentiment(self):
        """
        Calculate overall market sentiment based on all collected sources.

        This method computes the overall cryptocurrency market sentiment by:
        1. Calculating average sentiment from news sources
        2. Calculating average sentiment from social media (Reddit)
        3. Incorporating the Fear & Greed Index
        4. Combining these metrics with appropriate weights

        The weights are:
        - 50% news sentiment (most reliable indicator)
        - 30% social media sentiment (Reddit)
        - 20% Fear & Greed Index (market indicator)

        A source that returned nothing is left out and the remaining weights are
        renormalized, so an outage never counts as a neutral reading.

        Returns:
            float | None: The overall market sentiment score (0-100), or None
            when no source returned data
        """
        try:
            # Calculate social media sentiment (Reddit only, X.com removed)
            # Extract sentiment scores from Reddit sources
            social_scores = [
                s["sentiment"]
                for s in self.sentiment_data["sources"]
                if s["type"] == "reddit"
            ]
            social_sentiment = (
                sum(social_scores) / len(social_scores) if social_scores else None
            )

            # Calculate news sentiment
            # Extract sentiment scores from news sources
            news_scores = [
                s["sentiment"]
                for s in self.sentiment_data["sources"]
                if s["type"] == "news"
            ]
            news_sentiment = sum(news_scores) / len(news_scores) if news_scores else None

            fear_greed = self.fear_greed_index

            # Calculate overall sentiment as a weighted average of the components
            # that have data: 50% news, 30% Reddit, 20% fear & greed
            components = [
                (weight, value)
                for weight, value in (
                    (0.5, news_sentiment),
                    (0.3, social_sentiment),
                    (0.2, fear_greed),
                )
                if value is not None
            ]
            if not components:
                self.sentiment_data["overall"] = {}
                return None

            overall_sentiment = sum(weight * value for weight, value in components) / sum(
                weight for weight, _value in components
            )

            # Update the sentiment data structure with the calculated values
            self.sentiment_data["overall"] = {
                "sentiment": overall_sentiment,  # Combined weighted sentiment
                "social_sentiment": social_sentiment,  # Reddit sentiment
                "news_sentiment": news_sentiment,  # News sentiment
                "fear_greed_index": fear_greed,  # Fear & Greed Index
                "timestamp": utc_now().strftime("%Y-%m-%d %H:%M:%S"),  # Current timestamp
            }

            return overall_sentiment

        except Exception:
            logger.exception("Failed to calculate overall sentiment")
            self.sentiment_data["overall"] = {}
            return None

    def calculate_crypto_rankings(self):
        """
        Calculate sentiment rankings for cryptocurrencies.

        This method analyzes the sentiment data collected for each cryptocurrency and:
        1. Calculates the average sentiment score for each cryptocurrency
        2. Categorizes them as positive (≥50) or negative (<50)
        3. Ranks them within each category
        4. Returns the top 5 most positive and top 5 most negative cryptocurrencies

        The rankings provide insight into which cryptocurrencies currently have
        the most positive and most negative sentiment in the market.

        Returns:
            dict: A dictionary with two lists - 'positive' and 'negative' - each containing
                  the top 5 cryptocurrencies in that category with their sentiment data
        """
        try:
            # Initialize rankings structure with empty lists
            rankings = {"positive": [], "negative": []}

            # Process each cryptocurrency that has sentiment data
            for symbol, data in self.sentiment_data["crypto_specific"].items():
                # Skip cryptocurrencies with no sentiment scores
                if not data["sentiment_scores"]:
                    continue

                # Calculate average sentiment score for this cryptocurrency
                avg_sentiment = sum(data["sentiment_scores"]) / len(
                    data["sentiment_scores"]
                )

                # Calculate sentiment change (compared to historical data)
                # Currently set to 0 as we don't have historical comparison
                # This could be enhanced in the future to show sentiment trends
                sentiment_change = 0

                # Create data structure for this cryptocurrency
                crypto_data = {
                    "symbol": symbol,  # Cryptocurrency symbol (e.g., 'BTC')
                    "name": self.crypto_names.get(
                        symbol, symbol
                    ),  # Full name (e.g., 'Bitcoin')
                    "score": avg_sentiment,  # Average sentiment score (0-100)
                    "change": sentiment_change,  # Sentiment change (currently 0)
                    "sources_count": len(
                        data["sources"]
                    ),  # Number of sources mentioning this crypto
                }

                # Categorize as positive or negative based on sentiment score
                if avg_sentiment >= 50:  # 50 is neutral, ≥50 is positive
                    rankings["positive"].append(crypto_data)
                else:  # <50 is negative
                    rankings["negative"].append(crypto_data)

            # Sort the positive rankings by score (highest first)
            rankings["positive"] = sorted(
                rankings["positive"], key=lambda x: x["score"], reverse=True
            )

            # Sort the negative rankings by score (lowest first)
            rankings["negative"] = sorted(rankings["negative"], key=lambda x: x["score"])

            # Limit each category to the top 5 cryptocurrencies
            rankings["positive"] = rankings["positive"][:5]
            rankings["negative"] = rankings["negative"][:5]

            # Update the sentiment data with the rankings
            self.sentiment_data["rankings"] = rankings
            return rankings

        except Exception:
            return {"positive": [], "negative": []}  # Return empty rankings on error

    def cleanup_old_sentiment_data(self, max_age_days=None):
        """
        Clean up old sentiment snapshots from the database.

        This method removes sentiment snapshots that are older than the specified
        maximum age in days.

        Args:
            max_age_days (int, optional): Maximum age of snapshots to keep in
                days. Defaults to the configured retention window.

        Returns:
            int: The number of snapshots removed.
        """
        # Fall back to the configured retention window when not specified.
        if max_age_days is None:
            max_age_days = config.SENTIMENT_RETENTION_DAYS

        cutoff_time = utc_now() - timedelta(days=max_age_days)

        # Remove old snapshots and return how many were deleted
        return db.delete_sentiment_before(cutoff_time)

    def filter_decrypt_sources(self):
        """
        Filter out any sources from decrypt.co from the sentiment data.

        This method removes all news sources from decrypt.co from both the main
        sources list and from the cryptocurrency-specific sentiment data. It also
        recalculates sentiment scores to ensure they don't include data from
        the filtered sources.

        This filtering is done as per application requirements to exclude
        specific news sources from the sentiment analysis.

        Returns:
            bool: True if filtering was successful, False if an error occurred
        """
        try:
            # Filter out sources from decrypt.co from the main sources list
            # Keep only sources that are not from Decrypt and don't have decrypt.co in the URL
            filtered_sources = [
                source
                for source in self.sentiment_data["sources"]
                if source.get("source") != "Decrypt"
                and "decrypt.co" not in source.get("url", "")
            ]

            # Update the main sources list with the filtered list
            self.sentiment_data["sources"] = filtered_sources

            # Also update crypto-specific sentiment data to maintain consistency
            for crypto, data in self.sentiment_data["crypto_specific"].items():
                # Initialize new lists for scores and sources without decrypt.co content
                new_scores = []
                new_sources = []

                # Process each source and its corresponding sentiment score
                for i, source in enumerate(data.get("sources", [])):
                    # Skip sources from decrypt.co
                    if source.get("type") == "news" and "decrypt.co" in source.get(
                        "url", ""
                    ):
                        continue

                    # Keep the corresponding sentiment score if available
                    if i < len(data.get("sentiment_scores", [])):
                        new_scores.append(data["sentiment_scores"][i])

                    # Keep the source information
                    new_sources.append(source)

                # Update the crypto-specific data with filtered lists
                if new_scores or new_sources:
                    self.sentiment_data["crypto_specific"][crypto]["sentiment_scores"] = (
                        new_scores
                    )
                    self.sentiment_data["crypto_specific"][crypto]["sources"] = (
                        new_sources
                    )

            return True
        except Exception:
            return False

    def save_sentiment_data(self):
        """
        Save sentiment data to the database.

        This method inserts a new sentiment snapshot (the full sentiment data
        blob) with a creation timestamp. The most recent snapshot serves as the
        "latest" data used by the API, and historical snapshots are used for
        multi-day aggregations.

        Before saving, it ensures that any sources from decrypt.co are filtered out
        as per the application requirements.

        Returns:
            bool: True if the snapshot was saved successfully, False otherwise.
        """
        # With no real reading, keep the last good snapshot as the latest one.
        if not self.sentiment_data.get("overall"):
            logger.warning("No sentiment source returned data; nothing stored")
            return False

        try:
            # Filter out decrypt.co sources before saving
            self.filter_decrypt_sources()

            # Persist the sentiment snapshot to the database
            db.save_sentiment_snapshot(self.sentiment_data)

            # Clean up old sentiment snapshots (older than 30 days)
            try:
                self.cleanup_old_sentiment_data()
            except Exception:
                pass

            return True

        except Exception:
            logger.exception("Failed to save the sentiment snapshot")
            return False

    def run_scraper(self):
        """
        Run the complete sentiment scraping process.

        This method orchestrates the entire sentiment analysis workflow:
        1. Scrapes the Fear & Greed Index
        2. Collects news articles from cryptocurrency news sites
        3. Gathers sentiment data from Reddit
        4. Calculates overall market sentiment
        5. Ranks cryptocurrencies by sentiment
        6. Saves the collected data to JSON files

        Each step is executed in a separate try-except block to ensure that
        failure in one step doesn't prevent the execution of subsequent steps.

        Returns:
            dict: The complete sentiment data structure containing overall sentiment,
                  cryptocurrency-specific sentiment, and all sources
        """
        try:
            # Step 1: Scrape Fear & Greed Index (market sentiment indicator)
            self.scrape_fear_and_greed_index()
        except Exception:
            pass

        try:
            # Step 2: Scrape news from cryptocurrency news websites
            # Increased limit to get more news from various sources for better coverage
            self.scrape_crypto_news(limit=50)
        except Exception:
            pass

        try:
            # Step 3: Scrape sentiment data from Reddit cryptocurrency communities
            # Increased limit to match news sources for balanced sentiment analysis
            self.scrape_reddit_sentiment(limit=20)
        except Exception:
            pass

        try:
            # Step 4: Calculate overall market sentiment from all collected sources
            # This combines news, social media, and Fear & Greed Index with appropriate weights
            self.calculate_overall_sentiment()
        except Exception:
            pass

        try:
            # Step 5: Rank cryptocurrencies by sentiment (most positive and most negative)
            self.calculate_crypto_rankings()
        except Exception:
            pass

        try:
            # Step 6: Save all collected and processed data to JSON files
            # This saves both a timestamped file and updates the latest data file
            self.saved = self.save_sentiment_data()
        except Exception:
            pass

        return self.sentiment_data
