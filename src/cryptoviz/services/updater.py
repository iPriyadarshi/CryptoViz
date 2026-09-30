"""
Background data collection loops.

These run in the dedicated worker process (see cryptoviz.worker), not in the web
process. Keeping them out of the web server matters: gunicorn runs several
worker processes, and if the loops lived in the app each process would start its
own copy and multiply every scrape by the worker count.

Each loop is deliberately crash-proof - an exception is logged and the loop
sleeps and tries again, so one failing source never takes the collector down.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from .. import config
from ..db import repository as db
from ..scrapers import CryptoDataScraper, SentimentScraper, TopGainersScraper
from . import analytics

logger = logging.getLogger(__name__)


# How far back to recompute rollup buckets after each scrape. A trailing window
# rather than just the current bucket, so a scrape that lands either side of a
# boundary - or a brief collection gap - still leaves the recent buckets exact.
BUCKET_REFRESH_WINDOW_DAYS = 2


def collect_prices():
    """Scrape current prices, store them, refresh rollups and trim old data."""
    scraper = CryptoDataScraper()
    data = scraper.get_crypto_data()

    if not data:
        logger.warning("Price scrape returned no data")
        return 0

    db.save_prices(data)

    # Keep the hourly/daily rollups current so the long-range chart endpoints
    # stay in the millisecond range instead of re-aggregating the whole table.
    try:
        since = datetime.now() - timedelta(days=BUCKET_REFRESH_WINDOW_DAYS)
        db.refresh_price_buckets(since=since)
    except Exception:
        # A stale rollup degrades long-range charts; it must not fail the scrape,
        # whose raw rows are the source of truth.
        logger.exception("Failed to refresh price rollup buckets")

    analytics.cleanup_old_data()
    logger.info("Stored %d price rows", len(data))
    return len(data)


def collect_sentiment():
    """Run the sentiment scrape and trim old snapshots."""
    scraper = SentimentScraper()
    scraper.run_scraper()

    removed = scraper.cleanup_old_sentiment_data()
    logger.info("Sentiment snapshot stored (%d old snapshots removed)", removed or 0)
    return True


def collect_top_gainers():
    """Scrape the top gainers, store a snapshot and trim old ones."""
    scraper = TopGainersScraper()
    data = scraper.get_top_gainers()

    if not data:
        logger.warning("Top gainers scrape returned no data")
        return 0

    db.save_top_gainers(data)

    cutoff = datetime.now() - timedelta(days=config.TOP_GAINERS_RETENTION_DAYS)
    db.delete_top_gainers_before(cutoff)

    logger.info("Stored top gainers snapshot with %d entries", len(data))
    return len(data)


def _run_loop(name, task, interval, stop_event):
    """
    Run `task` every `interval` seconds until `stop_event` is set.

    Args:
        name (str): Loop name, used in log messages.
        task (callable): The collection function to run.
        interval (int): Seconds between runs.
        stop_event (threading.Event): Set to request a clean shutdown.
    """
    logger.info("Starting %s loop (every %ds)", name, interval)

    while not stop_event.is_set():
        started = time.monotonic()
        try:
            task()
        except Exception:
            # Logged with a traceback but never re-raised: a failing source must
            # not kill the collector.
            logger.exception("%s update failed", name)

        # Sleep on the event rather than time.sleep() so shutdown is immediate
        # instead of waiting out the full interval.
        elapsed = time.monotonic() - started
        stop_event.wait(max(0, interval - elapsed))

    logger.info("Stopped %s loop", name)


def start_all(stop_event=None):
    """
    Start every collection loop in its own daemon thread.

    Args:
        stop_event (threading.Event, optional): Shared shutdown signal. One is
            created when not supplied.

    Returns:
        tuple[list[threading.Thread], threading.Event]: The running threads and
        the event that stops them.
    """
    if stop_event is None:
        stop_event = threading.Event()

    loops = [
        ("price", collect_prices, config.PRICE_UPDATE_INTERVAL),
        ("sentiment", collect_sentiment, config.SENTIMENT_UPDATE_INTERVAL),
        ("top-gainers", collect_top_gainers, config.TOP_GAINERS_UPDATE_INTERVAL),
    ]

    threads = []
    for name, task, interval in loops:
        thread = threading.Thread(
            target=_run_loop,
            args=(name, task, interval, stop_event),
            name=f"{name}-updater",
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    return threads, stop_event
