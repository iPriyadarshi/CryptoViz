"""
Top gainers endpoints.
"""

import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

from .. import config
from ..db import repository as db

logger = logging.getLogger(__name__)

bp = Blueprint("top_gainers", __name__, url_prefix="/api/top-gainers")


@bp.route("", methods=["GET"])
@bp.route("/", methods=["GET"])
def get_top_gainers_data():
    """
    The most recent top gainers snapshot.

    Query Parameters:
        limit (int, optional): Maximum entries to return. Default 20.

    Returns:
        JSON: {'data': [...], 'last_updated': '...'}; 404 when no snapshot
        exists and one cannot be scraped on demand.
    """
    try:
        limit = request.args.get("limit", default=20, type=int)

        created_at, data = db.get_latest_top_gainers()

        # Nothing stored yet (fresh install before the worker's first run):
        # scrape once inline so the page has something to show.
        if not data:
            from ..scrapers import TopGainersScraper

            scraped = TopGainersScraper().get_top_gainers(limit)
            if not scraped:
                return (
                    jsonify(
                        {
                            "error": "No top gainers data available",
                            "message": (
                                "Failed to retrieve top gainers data. "
                                "Please try again later."
                            ),
                        }
                    ),
                    404,
                )
            db.save_top_gainers(scraped)
            created_at, data = db.get_latest_top_gainers()

        if limit and 0 < limit < len(data):
            data = data[:limit]

        last_updated = created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else None
        return jsonify({"data": data, "last_updated": last_updated})
    except Exception:
        logger.exception("Failed to load top gainers")
        return jsonify({"error": "Server error"}), 500


@bp.route("/update", methods=["GET"])
def trigger_top_gainers_update():
    """
    Manually trigger a top gainers refresh.

    The worker refreshes on a schedule; this backs the UI's refresh button.
    Recent data is left alone unless `force` is set.

    Query Parameters:
        force (bool, optional): Update even if the data is recent.

    Returns:
        JSON: status 'success' when an update was started, 'skipped' otherwise.
    """
    try:
        force = request.args.get("force", "false").lower() == "true"

        created_at, _ = db.get_latest_top_gainers()
        if created_at is not None and not force:
            age = (datetime.now() - created_at).total_seconds()
            if age < config.TOP_GAINERS_UPDATE_INTERVAL:
                return jsonify(
                    {
                        "message": (
                            f"Top gainers data is recent (updated "
                            f"{int(age / 60)} minutes ago). "
                            "Use force=true to update anyway."
                        ),
                        "status": "skipped",
                    }
                )

        def run_update():
            """Refresh top gainers off the request thread."""
            try:
                from ..services import updater

                updater.collect_top_gainers()
            except Exception:
                logger.exception("Manual top gainers update failed")

        threading.Thread(target=run_update, daemon=True).start()

        return jsonify(
            {"message": "Top gainers data update triggered", "status": "success"}
        )
    except Exception:
        logger.exception("Failed to trigger top gainers update")
        return jsonify({"error": "Server error"}), 500
