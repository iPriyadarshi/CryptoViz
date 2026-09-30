"""
Cryptocurrency price, volatility and correlation endpoints.
"""

import logging

from flask import Blueprint, jsonify, request

from .. import config
from ..services import analytics

logger = logging.getLogger(__name__)

bp = Blueprint("crypto", __name__, url_prefix="/api/crypto")


def _window_days(default=7):
    """
    Read the `days` query parameter, falling back on missing or invalid input.

    Values are clamped to the retention window: asking for more history than is
    kept would silently return a shorter series than the label promises.

    Returns:
        int: A positive number of days.
    """
    try:
        days = int(request.args.get("days", default))
    except TypeError, ValueError:
        return default
    if days <= 0:
        return default
    return min(days, config.PRICE_RETENTION_DAYS)


@bp.route("", methods=["GET"])
@bp.route("/", methods=["GET"])
def get_crypto():
    """
    Latest data for all tracked cryptocurrencies.

    Returns:
        JSON: {'data': [ ... ]}
    """
    try:
        return jsonify({"data": analytics.get_latest_data()})
    except Exception:
        logger.exception("Failed to load latest crypto data")
        return jsonify({"error": "Server error"}), 500


@bp.route("/<symbol>/history", methods=["GET"])
def get_crypto_history(symbol):
    """
    Historical price data for one cryptocurrency.

    Query Parameters:
        days (int, optional): Window size in days. Defaults to 1 (24 hours).
            Long windows are downsampled server-side, so a one-year request
            returns a few hundred points rather than the ~105,000 rows stored.

    Args:
        symbol (str): The cryptocurrency symbol (e.g. 'btc').

    Returns:
        JSON: {'prices': [...], 'timestamps': [...], 'days': n, 'bucket': ...}
    """
    try:
        days = _window_days(default=1)
        history = analytics.get_history(symbol, days=days)

        # Tell the client what it actually got, so the chart can label the axis
        # by day rather than by minute when the series was bucketed.
        history["days"] = days
        history["bucket"] = analytics.bucket_for_window(days)
        return jsonify(history)
    except Exception:
        logger.exception("Failed to load history for %s", symbol)
        return jsonify({"error": "Server error"}), 500


@bp.route("/<symbol>/volatility", methods=["GET"])
def get_crypto_volatility(symbol):
    """
    Volatility metrics for one cryptocurrency.

    Query Parameters:
        days (int, optional): Window size in days. Default 7.

    Returns:
        JSON: volatility, max_drawdown, daily_returns, timestamps.
    """
    try:
        return jsonify(analytics.calculate_volatility(symbol, _window_days()))
    except Exception:
        logger.exception("Failed to calculate volatility for %s", symbol)
        return jsonify({"error": "Server error"}), 500


@bp.route("/volatility", methods=["GET"])
def get_all_crypto_volatility():
    """
    Volatility metrics for every tracked cryptocurrency.

    Query Parameters:
        days (int, optional): Window size in days. Default 7.

    Returns:
        JSON: {'data': [ ... ]} sorted by volatility descending.
    """
    try:
        return jsonify({"data": analytics.get_all_volatility(_window_days())})
    except Exception:
        logger.exception("Failed to calculate volatility for all symbols")
        return jsonify({"error": "Server error"}), 500


@bp.route("/correlation", methods=["GET"])
def get_correlation_matrix():
    """
    Price-return correlation matrix across cryptocurrencies.

    Query Parameters:
        days (int, optional): Window size in days. Default 7.

    Returns:
        JSON: {'correlation_matrix': [[...]], 'symbols': [...]}. Undefined
        pairs are null.
    """
    try:
        return jsonify(analytics.calculate_correlation_matrix(_window_days()))
    except Exception:
        logger.exception("Failed to calculate correlation matrix")
        return jsonify({"error": "Server error"}), 500
