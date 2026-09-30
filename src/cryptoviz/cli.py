"""
Console entry points.

These are what pyproject.toml exposes as `cryptoviz-web`, `cryptoviz-worker`,
`cryptoviz-backfill` and `cryptoviz-scrape-once`, so every way of running the
project is a named command rather than a path to a script.
"""

import logging
import sys

from . import config
from .logging_config import configure_logging

logger = logging.getLogger(__name__)


def run_web():
    """
    Run the Flask development server.

    Development only - production uses gunicorn against cryptoviz.wsgi:app.
    """
    from .app import create_app

    app = create_app()
    logger.info("Serving on http://%s:%s", config.HOST, config.PORT)
    app.run(debug=config.DEBUG, host=config.HOST, port=config.PORT)
    return 0


def run_worker():
    """Run the background scraper worker."""
    from .worker import main

    return main()


def run_backfill():
    """
    Backfill historical prices from CoinGecko.

    Accepts an optional number of days as the single argument; without it the
    configured retention window is used.

        cryptoviz-backfill          # the full retention window
        cryptoviz-backfill 90       # just the last 90 days
    """
    configure_logging()
    from .scrapers.historical import main

    days = None
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            logger.error("Usage: cryptoviz-backfill [days]")
            return 2

    return main(days=days)


def rebuild_rollups():
    """
    Rebuild the hourly and daily price rollups from the raw rows.

    The worker does this at startup and incrementally after each scrape, so this
    is only needed after restoring a database or importing history by some other
    route.
    """
    configure_logging()

    from .db import init_db
    from .db import repository as db

    init_db()
    written = db.refresh_price_buckets()
    logger.info("Rollup buckets written: %s", written)
    return 0


def scrape_once():
    """
    Run one round of every scraper and exit.

    Useful for seeding a fresh database or checking that the sources still
    parse, without leaving the worker running.
    """
    configure_logging()

    from .db import init_db
    from .services import updater

    init_db()

    exit_code = 0
    for name, task in (
        ("prices", updater.collect_prices),
        ("sentiment", updater.collect_sentiment),
        ("top gainers", updater.collect_top_gainers),
    ):
        try:
            task()
        except Exception:
            logger.exception("One-off %s scrape failed", name)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(run_web())
