"""
Market sentiment endpoints.

Sentiment is stored as whole JSON snapshots rather than normalized rows, so
these handlers read the latest snapshot (or a range of them) and reshape it for
the frontend. They are read-only: the worker process owns collection.
"""

import logging
from datetime import timedelta

from flask import Blueprint, jsonify, request

from .. import config
from ..db import repository as db
from ..db.models import utc_now
from ..services import analytics

logger = logging.getLogger(__name__)

bp = Blueprint("sentiment", __name__, url_prefix="/api/sentiment")

# Time ranges the frontend offers. Anything else falls back to the default.
ALLOWED_DAYS = config.ALLOWED_WINDOW_DAYS

# Components of an overall reading. A source that was unavailable is null.
SENTIMENT_KEYS = (
    "sentiment",
    "social_sentiment",
    "news_sentiment",
    "fear_greed_index",
)

# Most sources a single response may carry.
MAX_SOURCES = 500

_NO_DATA = {
    "error": "No sentiment data available",
    "message": "Sentiment data is being collected. Please try again later.",
}


def _requested_days(default=1):
    """Read and validate the `days` query parameter."""
    try:
        days = int(request.args.get("days", default))
    except TypeError, ValueError:
        return default
    return days if days in ALLOWED_DAYS else default


@bp.route("", methods=["GET"])
@bp.route("/", methods=["GET"])
def get_sentiment():
    """
    The latest complete sentiment snapshot.

    Returns:
        JSON: overall sentiment, per-cryptocurrency sentiment, rankings and all
        sources; 404 when nothing has been collected yet.
    """
    try:
        data = db.get_latest_sentiment()
        if data is None:
            return jsonify(_NO_DATA), 404
        return jsonify(data)
    except Exception:
        logger.exception("Failed to load latest sentiment")
        return jsonify({"error": "Server error"}), 500


@bp.route("/overall", methods=["GET"])
def get_overall_sentiment():
    """
    Overall market sentiment, averaged across the requested window.

    Query Parameters:
        days (int, optional): 1, 7, 14, 30, 90 or 365. Default 1 (the latest
            snapshot).

    Returns:
        JSON: sentiment, social_sentiment, news_sentiment, fear_greed_index.
    """
    try:
        days = _requested_days()

        # A one-day window is just the newest snapshot; no averaging needed.
        if days == 1:
            data = db.get_latest_sentiment(source_limit=0)
            if data is None:
                return jsonify(_NO_DATA), 404
            return jsonify(data.get("overall", {}))

        cutoff = utc_now() - timedelta(days=days)
        averages = db.get_sentiment_averages(cutoff, SENTIMENT_KEYS)

        if averages is None:
            data = db.get_latest_sentiment(source_limit=0)
            if data is None:
                return jsonify(_NO_DATA), 404
            return jsonify(data.get("overall", {}))

        return jsonify({**averages, "period": f"{days}d"})
    except Exception:
        logger.exception("Failed to load overall sentiment")
        return jsonify({"error": "Server error"}), 500


@bp.route("/rankings", methods=["GET"])
def get_sentiment_rankings():
    """
    Most positive and most negative cryptocurrencies by sentiment.

    Query Parameters:
        days (int, optional): Accepted for symmetry with the other endpoints.

    Returns:
        JSON: {'positive': [...], 'negative': [...]}
    """
    empty = {"positive": [], "negative": []}

    try:
        # Rankings are not meaningfully averageable, so every window shows the
        # newest reading.
        data = db.get_latest_sentiment(source_limit=0)
        if data is None:
            return jsonify(_NO_DATA), 404
        return jsonify(data.get("rankings", empty))
    except Exception:
        logger.exception("Failed to load sentiment rankings")
        return jsonify({"error": "Server error"}), 500


@bp.route("/sources", methods=["GET"])
def get_sentiment_sources():
    """
    The raw articles and posts behind the sentiment scores.

    Sources are stored de-duplicated in their own table, so every filter here
    runs as an indexed SQL query. The previous approach - loading every
    snapshot in the range and de-duplicating in Python - would mean reading
    hundreds of megabytes of JSON for a long window.

    Query Parameters:
        type (str, optional): Filter by source type ('news', 'reddit').
        crypto (str, optional): Only sources mentioning this symbol.
        limit (int, optional): Maximum number of sources to return, capped at
            MAX_SOURCES (also the default).
        days (int, optional): Window size in days. Default 1.

    Returns:
        JSON: {'sources': [...], 'count': n, 'total': n}
    """
    try:
        days = _requested_days()
        cutoff = utc_now() - timedelta(days=days)

        source_type = request.args.get("type")
        crypto = request.args.get("crypto")
        limit = request.args.get("limit", type=int)

        sources = db.query_sentiment_sources(
            since=cutoff,
            source_type=source_type,
            crypto=crypto,
            limit=min(limit, MAX_SOURCES) if limit and limit > 0 else MAX_SOURCES,
        )

        # `total` is how many match before the limit, so the UI can say
        # "showing 20 of 340" without a second request.
        total = db.count_sentiment_sources(
            since=cutoff, source_type=source_type, crypto=crypto
        )

        return jsonify({"sources": sources, "count": len(sources), "total": total})
    except Exception:
        logger.exception("Failed to load sentiment sources")
        return jsonify({"error": "Server error"}), 500


@bp.route("/trends", methods=["GET"])
def get_sentiment_trends():
    """
    Sentiment plotted against price over time.

    Query Parameters:
        days (int, optional): 1, 7, 14, 30, 90 or 365. Default 7.
        symbol (str, optional): Cryptocurrency to chart. Default 'BTC'; 'all'
            charts overall market sentiment against the BTC price.

    Returns:
        JSON: {'dates': [...], 'sentiment': [...], 'price': [...]}
    """
    try:
        days = _requested_days(default=7)
        symbol = request.args.get("symbol", default="BTC").strip()
        is_market = symbol.lower() == "all"

        # Long windows keep one snapshot per bucket, so the chart stays readable
        # and the request does not parse a year of snapshots.
        bucket = analytics.trend_bucket_for_window(days)
        cutoff = utc_now() - timedelta(days=days)
        snapshots = db.get_sentiment_snapshots_since(cutoff, bucket=bucket)

        trend = {"dates": [], "sentiment": [], "price": []}
        if not snapshots:
            return jsonify(trend)

        # Load the price series once and align all snapshots against it, so a
        # long request costs one query instead of one per data point.
        price_symbol = "btc" if is_market else symbol.lower()
        series = analytics.get_price_series(price_symbol, since=cutoff, bucket=bucket)

        # Fall back to the full history if nothing was recorded in the window.
        if series.empty:
            series = analytics.get_price_series(price_symbol, bucket=bucket)

        # Last resort: a flat line at the current price beats an empty chart.
        fallback_price = None
        if series.empty:
            fallback_price = next(
                (
                    row["price"]
                    for row in analytics.get_latest_data()
                    if row["symbol"] == price_symbol
                ),
                None,
            )

        points = []
        for created_at, data in snapshots:
            try:
                if is_market:
                    sentiment_value = data.get("overall", {}).get("sentiment")
                else:
                    crypto_data = data.get("crypto_specific", {}).get(symbol.upper(), {})
                    scores = crypto_data.get("sentiment_scores") if crypto_data else None
                    sentiment_value = sum(scores) / len(scores) if scores else None
            except Exception:
                logger.debug("Skipping malformed sentiment snapshot", exc_info=True)
                continue

            # No reading in this snapshot: leave a gap rather than plot a
            # made-up neutral value.
            if sentiment_value is not None:
                points.append((created_at, sentiment_value))

        if not points:
            return jsonify(trend)

        prices = analytics.prices_at(series, [created_at for created_at, _ in points])
        for (created_at, sentiment_value), price in zip(points, prices, strict=True):
            trend["dates"].append(created_at.strftime("%Y-%m-%d %H:%M"))
            trend["sentiment"].append(sentiment_value)
            trend["price"].append(price if price is not None else fallback_price)

        return jsonify(trend)
    except Exception:
        logger.exception("Failed to build sentiment trends")
        return jsonify({"error": "Server error"}), 500
