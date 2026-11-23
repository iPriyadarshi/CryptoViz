"""
Top gainers endpoints.

Read-only: the worker process owns collection, so no request can start a scrape
or write to the database.
"""

import logging

from flask import Blueprint, jsonify, request

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
        has been collected yet.
    """
    try:
        limit = request.args.get("limit", default=20, type=int)

        created_at, data = db.get_latest_top_gainers()

        if not data:
            return (
                jsonify(
                    {
                        "error": "No top gainers data available",
                        "message": (
                            "Top gainers data is being collected. Please try again later."
                        ),
                    }
                ),
                404,
            )

        if limit and 0 < limit < len(data):
            data = data[:limit]

        last_updated = created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else None
        return jsonify({"data": data, "last_updated": last_updated})
    except Exception:
        logger.exception("Failed to load top gainers")
        return jsonify({"error": "Server error"}), 500
