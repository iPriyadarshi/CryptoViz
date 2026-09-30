"""
Scraper worker process.

Runs the price, sentiment and top-gainers collection loops. This is a separate
process from the web server, managed by its own systemd unit, for two reasons:

  * gunicorn runs multiple web workers, and loops living inside the app would be
    duplicated once per worker - every scrape multiplied by the worker count.
  * collection and serving can then be restarted, throttled and debugged
    independently.

Both processes talk to the same SQLite database; see db/engine.py for the WAL
and busy-timeout settings that make that concurrent access safe.
"""

import logging
import signal
import threading

from .db import init_db
from .db import repository as db
from .logging_config import configure_logging
from .services import updater

logger = logging.getLogger(__name__)


def main():
    """
    Run the collection loops until the process is asked to stop.

    Returns:
        int: Process exit code.
    """
    configure_logging()
    logger.info("Starting CryptoViz scraper worker")

    init_db()

    # Rebuild the price rollups once up front. The per-scrape refresh only
    # covers a short trailing window, so this is what makes the buckets
    # correct again after downtime, a restore, or a history backfill.
    try:
        written = db.refresh_price_buckets()
        logger.info("Rebuilt price rollups: %s", written)
    except Exception:
        logger.exception("Failed to rebuild price rollups at startup")

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        """Ask the loops to finish their current cycle and exit."""
        logger.info("Received signal %s; shutting down", signum)
        stop_event.set()

    # systemd sends SIGTERM on stop/restart; SIGINT covers Ctrl-C when run by
    # hand. Both trigger the same clean shutdown.
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    threads, _ = updater.start_all(stop_event)

    # Block until a signal arrives, then let each loop finish its cycle.
    stop_event.wait()

    for thread in threads:
        thread.join(timeout=30)

    logger.info("Scraper worker stopped")
    return 0
