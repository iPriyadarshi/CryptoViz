"""
Market sentiment endpoints.

Sentiment is stored as whole JSON snapshots rather than normalized rows, so
these handlers read the latest snapshot (or a range of them) and reshape it for
the frontend.
"""

import logging
import threading
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from .. import config
from ..db import repository as db
from ..services import analytics

logger = logging.getLogger(__name__)

bp = Blueprint("sentiment", __name__, url_prefix="/api/sentiment")

# Time ranges the frontend offers. Anything else falls back to the default.
ALLOWED_DAYS = config.ALLOWED_WINDOW_DAYS

# Neutral reading used when no sentiment data exists yet.
NEUTRAL_SENTIMENT = {
    "sentiment": 50,
    "social_sentiment": 50,
    "news_sentiment": 50,
    "fear_greed_index": 50,
}

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
        days (int, optional): 1, 7 or 30. Default 1 (the latest snapshot).

    Returns:
        JSON: sentiment, social_sentiment, news_sentiment, fear_greed_index.
    """
    try:
        days = _requested_days()

        # A one-day window is just the newest snapshot; no averaging needed.
        if days == 1:
            data = db.get_latest_sentiment()
            if data is None:
                return jsonify(_NO_DATA), 404
            return jsonify(data.get("overall", {}))

        cutoff = datetime.now() - timedelta(days=days)
        snapshots = db.get_sentiment_snapshots_since(cutoff)

        if not snapshots:
            data = db.get_latest_sentiment()
            if data is not None:
                return jsonify(data.get("overall", {}))
            return jsonify(
                {
                    **NEUTRAL_SENTIMENT,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

        # Average each component across every snapshot in the window.
        totals = dict.fromkeys(NEUTRAL_SENTIMENT, 0)
        count = 0
        for _created_at, data in snapshots:
            overall = data.get("overall") or {}
            if not overall:
                continue
            for key in totals:
                totals[key] += overall.get(key, NEUTRAL_SENTIMENT[key])
            count += 1

        if count:
            averages = {key: total / count for key, total in totals.items()}
        else:
            averages = dict(NEUTRAL_SENTIMENT)

        # Snapshots come back oldest-first, so the last one is the most recent.
        latest_timestamp = snapshots[-1][0].strftime("%Y-%m-%d %H:%M:%S")

        return jsonify(
            {
                **averages,
                "timestamp": latest_timestamp,
                "period": f"{days}d",
                "data_points": count,
            }
        )
    except Exception:
        logger.exception("Failed to load overall sentiment")
        return jsonify({"error": "Server error"}), 500


@bp.route("/rankings", methods=["GET"])
def get_sentiment_rankings():
    """
    Most positive and most negative cryptocurrencies by sentiment.

    Query Parameters:
        days (int, optional): 1, 7 or 30. Default 1.

    Returns:
        JSON: {'positive': [...], 'negative': [...]}
    """
    empty = {"positive": [], "negative": []}

    try:
        days = _requested_days()

        if days == 1:
            data = db.get_latest_sentiment()
            if data is None:
                return jsonify(_NO_DATA), 404
            return jsonify(data.get("rankings", empty))

        cutoff = datetime.now() - timedelta(days=days)
        snapshots = db.get_sentiment_snapshots_since(cutoff)

        if not snapshots:
            data = db.get_latest_sentiment()
            return jsonify(data.get("rankings", empty) if data else empty)

        # Rankings are not meaningfully averageable, so use the newest snapshot
        # that falls inside the requested window.
        _created_at, latest_in_range = snapshots[-1]
        return jsonify(latest_in_range.get("rankings", empty))
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
        limit (int, optional): Maximum number of sources to return.
        days (int, optional): Window size in days. Default 1.

    Returns:
        JSON: {'sources': [...], 'count': n, 'total': n}
    """
    try:
        days = _requested_days()
        cutoff = datetime.now() - timedelta(days=days)

        source_type = request.args.get("type")
        crypto = request.args.get("crypto")
        limit = request.args.get("limit", type=int)

        sources = db.query_sentiment_sources(
            since=cutoff,
            source_type=source_type,
            crypto=crypto,
            limit=limit if limit and limit > 0 else None,
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
        days (int, optional): 1, 7 or 30. Default 7.
        symbol (str, optional): Cryptocurrency to chart. Default 'BTC';
            'all' uses BTC as the market proxy.

    Returns:
        JSON: {'dates': [...], 'sentiment': [...], 'price': [...]}
    """
    try:
        days = _requested_days(default=7)
        symbol = request.args.get("symbol", default="BTC")

        cutoff = datetime.now() - timedelta(days=days)
        snapshots = db.get_sentiment_snapshots_since(cutoff)

        trend = {"dates": [], "sentiment": [], "price": []}
        if not snapshots:
            return jsonify(trend)

        # Load the price series once and look every snapshot up against it, so a
        # 30-day request costs one query instead of one per data point.
        price_symbol = "btc" if symbol == "all" else symbol.lower()
        bucket = analytics.bucket_for_window(days)
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

        for created_at, data in snapshots:
            try:
                trend["dates"].append(created_at.strftime("%Y-%m-%d %H:%M"))

                if symbol in ("all", "BTC"):
                    sentiment_value = data.get("overall", {}).get("sentiment", 50)
                else:
                    crypto_data = data.get("crypto_specific", {}).get(symbol.upper(), {})
                    scores = crypto_data.get("sentiment_scores") if crypto_data else None
                    sentiment_value = sum(scores) / len(scores) if scores else 50

                trend["sentiment"].append(sentiment_value)

                price = analytics.price_at(series, created_at)
                trend["price"].append(price if price is not None else fallback_price)
            except Exception:
                logger.debug("Skipping malformed sentiment snapshot", exc_info=True)
                continue

        return jsonify(trend)
    except Exception:
        logger.exception("Failed to build sentiment trends")
        return jsonify({"error": "Server error"}), 500


@bp.route("/update", methods=["GET"])
def trigger_sentiment_update():
    """
    Manually trigger a sentiment refresh.

    The worker process refreshes sentiment on a schedule already; this exists so
    the UI's refresh control can ask for an early update. Recent data is left
    alone unless `force` is set, to keep a page reload from hammering the
    upstream sources.

    Query Parameters:
        force (bool, optional): Update even if the data is recent.

    Returns:
        JSON: status 'success' when an update was started, 'skipped' otherwise.
    """
    try:
        force = request.args.get("force", "false").lower() == "true"

        latest_time = db.get_latest_sentiment_time()
        if latest_time is not None and not force:
            age = (datetime.now() - latest_time).total_seconds()
            if age < config.SENTIMENT_UPDATE_INTERVAL:
                return jsonify(
                    {
                        "message": (
                            f"Sentiment data is recent (updated {int(age / 60)} "
                            "minutes ago). Use force=true to update anyway."
                        ),
                        "status": "skipped",
                    }
                )

        def run_update():
            """Refresh sentiment off the request thread."""
            try:
                from ..services import updater

                updater.collect_sentiment()
            except Exception:
                logger.exception("Manual sentiment update failed")

        threading.Thread(target=run_update, daemon=True).start()

        return jsonify(
            {"message": "Sentiment data update triggered", "status": "success"}
        )
    except Exception:
        logger.exception("Failed to trigger sentiment update")
        return jsonify({"error": "Server error"}), 500
