"""
CryptoViz Application

This is the unified Flask application for the CryptoViz platform. It serves both
the frontend (static assets and HTML pages) and the RESTful API from a single
same-origin server.

API endpoints provide cryptocurrency data, historical prices, volatility metrics,
correlation analysis, sentiment data, and top gainers. All data is persisted in a
database (see database.py) - by default a Turso (libSQL / cloud SQLite) database,
falling back to a local SQLite file when Turso is not configured.

The application also manages background threads for periodically updating
cryptocurrency prices, sentiment information, and top gainers.
"""

from flask import Flask, jsonify, request, render_template, abort
from flask_cors import CORS
from scraper import (
    TopGainersScraper,
    DataProcessor,
)
from data_utils import (
    get_latest_data,
    get_history,
    calculate_volatility,
    get_all_volatility,
    calculate_correlation_matrix,
    update_data_periodically,
    update_sentiment_data_periodically,
    update_top_gainers_periodically,
    get_closest_price_for_timestamp,
    get_latest_price_before_timestamp,
    get_earliest_price_after_timestamp,
)
from sentiment_scraper import SentimentScraper
import database as db
import threading
import time
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Initialize Flask application.
# static_url_path="" serves everything under static/ at the site root, so the
# frontend's absolute references (/css/..., /js/..., /assets/...) resolve without
# modification. HTML pages live in templates/ and are served by explicit routes.
app = Flask(__name__, static_folder="static", static_url_path="", template_folder="templates")

# Enable Cross-Origin Resource Sharing for the API (frontend is same-origin, but
# this keeps the API usable from other origins if needed).
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Ensure database tables exist as soon as the app is imported (works under any
# WSGI server, not just when run directly).
try:
    db.init_db()
except Exception as e:
    print(f"Warning: could not initialize database on startup: {e}")


# ---------------------------------------------------------------------------
# Frontend routes
# ---------------------------------------------------------------------------

# HTML pages that make up the frontend (served from templates/).
FRONTEND_PAGES = [
    "index",
    "normalized-trend",
    "volatility",
    "correlation",
    "sentiment",
    "top-gainers",
    "website-info",
    "about",
]


@app.route("/")
def home():
    """Serve the main dashboard (homepage)."""
    return render_template("index.html")


def _make_page_view(template_name):
    """Build a view function that renders a specific frontend template."""

    def view():
        return render_template(template_name)

    return view


# Register an explicit route for each frontend page (e.g. /volatility.html).
# Explicit string rules take precedence over the static file catch-all.
for _page in FRONTEND_PAGES:
    app.add_url_rule(
        f"/{_page}.html",
        endpoint=f"page_{_page}",
        view_func=_make_page_view(f"{_page}.html"),
    )


@app.route("/api-docs")
def api_docs():
    """Serve the API documentation page."""
    return render_template("api_docs.html")


# ---------------------------------------------------------------------------
# Cryptocurrency price API
# ---------------------------------------------------------------------------


@app.route("/api/crypto", methods=["GET"])
def get_crypto():
    """
    API endpoint to get the latest cryptocurrency data.

    Returns:
        JSON response containing the most recent data for all tracked cryptocurrencies.
    """
    try:
        # Get the most recent cryptocurrency data from the database
        data = get_latest_data()
        return jsonify({"data": data})
    except Exception:
        return jsonify({"error": "Server error"}), 500


@app.route("/api/crypto/<symbol>/history", methods=["GET"])
def get_crypto_history(symbol):
    """
    API endpoint to get historical price data for a specific cryptocurrency.

    Args:
        symbol (str): The cryptocurrency symbol (e.g., 'btc', 'eth')

    Returns:
        JSON response containing historical price data for the specified cryptocurrency.
    """
    try:
        # Get historical data for the specified cryptocurrency
        history = get_history(symbol)
        return jsonify(history)
    except Exception:
        return jsonify({"error": "Server error"}), 500


@app.route("/api/crypto/<symbol>/volatility", methods=["GET"])
def get_crypto_volatility(symbol):
    """
    API endpoint to get volatility metrics for a specific cryptocurrency.

    Args:
        symbol (str): The cryptocurrency symbol (e.g., 'btc', 'eth')

    Query Parameters:
        days (int, optional): Number of days to calculate volatility for. Default is 7.

    Returns:
        JSON response containing volatility metrics for the specified cryptocurrency.
    """
    try:
        # Get the number of days from query parameters (default: 7)
        days = request.args.get("days", default=7, type=int)

        # Calculate volatility metrics for the specified cryptocurrency and time period
        volatility_data = calculate_volatility(symbol, days)
        return jsonify(volatility_data)
    except Exception:
        return jsonify({"error": "Server error"}), 500


@app.route("/api/crypto/volatility", methods=["GET"])
def get_all_crypto_volatility():
    """
    API endpoint to get volatility data for all tracked cryptocurrencies.

    Query Parameters:
        days (int, optional): Number of days to calculate volatility for. Default is 7.

    Returns:
        JSON response containing volatility metrics for all cryptocurrencies.
    """
    try:
        # Get days parameter and ensure it's a valid integer
        days_param = request.args.get("days", default="7")
        try:
            days = int(days_param)
            if days <= 0:
                days = 7  # Default to 7 if invalid
        except (ValueError, TypeError):
            days = 7  # Default to 7 if conversion fails

        # Get volatility data for all cryptocurrencies for the specified time period
        volatility_data = get_all_volatility(days)

        # Ensure we have valid data
        if not volatility_data:
            volatility_data = []

        return jsonify({"data": volatility_data})
    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


@app.route("/api/crypto/correlation", methods=["GET"])
def get_correlation_matrix():
    """
    API endpoint to get the price correlation matrix for all cryptocurrencies.

    Query Parameters:
        days (int, optional): Number of days to calculate correlation for. Default is 7.

    Returns:
        JSON response containing the correlation matrix and list of cryptocurrency symbols.
    """
    try:
        # Get days parameter and ensure it's a valid integer
        days_param = request.args.get("days", default="7")
        try:
            days = int(days_param)
            if days <= 0:
                days = 7  # Default to 7 if invalid
        except (ValueError, TypeError):
            days = 7  # Default to 7 if conversion fails

        # Calculate the correlation matrix for the specified time period
        correlation_data = calculate_correlation_matrix(days)

        # Ensure we have valid data structure
        if (
            not correlation_data
            or "correlation_matrix" not in correlation_data
            or "symbols" not in correlation_data
        ):
            correlation_data = {"correlation_matrix": [], "symbols": []}

        # Additional validation to ensure no NaN values in the matrix
        if (
            "correlation_matrix" in correlation_data
            and correlation_data["correlation_matrix"]
        ):
            import numpy as np
            import json

            # Convert any string "NaN" to None for proper JSON serialization
            for i, row in enumerate(correlation_data["correlation_matrix"]):
                for j, val in enumerate(row):
                    if val == "NaN" or (isinstance(val, float) and np.isnan(val)):
                        correlation_data["correlation_matrix"][i][j] = None

            # Verify JSON serialization works
            try:
                # Test if the data can be serialized to JSON
                json.dumps(correlation_data)
            except TypeError:
                # If serialization fails, create a clean structure
                correlation_data = {"correlation_matrix": [], "symbols": []}

        return jsonify(correlation_data)
    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Sentiment API
# ---------------------------------------------------------------------------


@app.route("/api/sentiment", methods=["GET"])
def get_sentiment():
    """
    API endpoint to get the latest complete sentiment data.

    Returns the full sentiment data including overall sentiment,
    cryptocurrency-specific sentiment, rankings, and all sentiment sources.
    """
    try:
        # Get the most recent sentiment snapshot from the database
        sentiment_data = db.get_latest_sentiment()

        if sentiment_data is None:
            return (
                jsonify(
                    {
                        "error": "No sentiment data available",
                        "message": "Sentiment data is being collected. Please try again later.",
                    }
                ),
                404,
            )

        return jsonify(sentiment_data)
    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


@app.route("/api/sentiment/overall", methods=["GET"])
def get_overall_sentiment():
    """
    API endpoint to get overall market sentiment data.

    Query Parameters:
        days (int, optional): Time period for sentiment data (1, 7, or 30 days). Default is 1.

    Returns:
        JSON response containing overall sentiment metrics.
    """
    try:
        # Get the requested time range (default to 1 day if not specified)
        days = request.args.get("days", default=1, type=int)

        # If days=1, just return the latest data
        if days == 1:
            sentiment_data = db.get_latest_sentiment()
            if sentiment_data is None:
                return (
                    jsonify(
                        {
                            "error": "No sentiment data available",
                            "message": "Sentiment data is being collected. Please try again later.",
                        }
                    ),
                    404,
                )
            return jsonify(sentiment_data.get("overall", {}))

        # For longer time ranges (7d, 30d), average across snapshots in range
        cutoff = datetime.now() - timedelta(days=days)
        snapshots = db.get_sentiment_snapshots_since(cutoff)

        # If no snapshots found, return the latest data or default values
        if not snapshots:
            sentiment_data = db.get_latest_sentiment()
            if sentiment_data is not None:
                return jsonify(sentiment_data.get("overall", {}))
            return jsonify(
                {
                    "sentiment": 50,
                    "social_sentiment": 50,
                    "news_sentiment": 50,
                    "fear_greed_index": 50,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

        # Use the timestamp from the most recent snapshot (snapshots are oldest-first)
        latest_timestamp = snapshots[-1][0].strftime("%Y-%m-%d %H:%M:%S")

        # Accumulate the sentiment values to compute averages
        sentiment_sum = 0
        social_sentiment_sum = 0
        news_sentiment_sum = 0
        fear_greed_sum = 0
        count = 0

        for _created_at, data in snapshots:
            overall = data.get("overall", {})
            if overall:
                sentiment_sum += overall.get("sentiment", 50)
                social_sentiment_sum += overall.get("social_sentiment", 50)
                news_sentiment_sum += overall.get("news_sentiment", 50)
                fear_greed_sum += overall.get("fear_greed_index", 50)
                count += 1

        if count > 0:
            avg_sentiment = sentiment_sum / count
            avg_social_sentiment = social_sentiment_sum / count
            avg_news_sentiment = news_sentiment_sum / count
            avg_fear_greed = fear_greed_sum / count
        else:
            avg_sentiment = 50
            avg_social_sentiment = 50
            avg_news_sentiment = 50
            avg_fear_greed = 50

        overall_sentiment = {
            "sentiment": avg_sentiment,
            "social_sentiment": avg_social_sentiment,
            "news_sentiment": avg_news_sentiment,
            "fear_greed_index": avg_fear_greed,
            "timestamp": latest_timestamp,
            "period": f"{days}d",
            "data_points": count,
        }

        return jsonify(overall_sentiment)
    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


@app.route("/api/sentiment/rankings", methods=["GET"])
def get_sentiment_rankings():
    """
    API endpoint to get sentiment rankings for cryptocurrencies.

    Query Parameters:
        days (int, optional): Time period for sentiment data (1, 7, or 30 days). Default is 1.

    Returns:
        JSON response containing positive and negative sentiment rankings.
    """
    try:
        # Get the requested time range (default to 1 day if not specified)
        days = request.args.get("days", default=1, type=int)

        # Validate days parameter (only allow 1, 7, or 30 days)
        if days not in [1, 7, 30]:
            days = 1

        # If days=1, just return the latest data
        if days == 1:
            sentiment_data = db.get_latest_sentiment()
            if sentiment_data is None:
                return (
                    jsonify(
                        {
                            "error": "No sentiment data available",
                            "message": "Sentiment data is being collected. Please try again later.",
                        }
                    ),
                    404,
                )
            return jsonify(
                sentiment_data.get("rankings", {"positive": [], "negative": []})
            )

        # For longer time ranges, use the most recent snapshot within the range
        cutoff = datetime.now() - timedelta(days=days)
        snapshots = db.get_sentiment_snapshots_since(cutoff)

        if not snapshots:
            sentiment_data = db.get_latest_sentiment()
            if sentiment_data is not None:
                return jsonify(
                    sentiment_data.get("rankings", {"positive": [], "negative": []})
                )
            return jsonify({"positive": [], "negative": []})

        # snapshots are oldest-first; the most recent is the last element
        _created_at, latest_in_range = snapshots[-1]
        return jsonify(
            latest_in_range.get("rankings", {"positive": [], "negative": []})
        )
    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


@app.route("/api/sentiment/sources", methods=["GET"])
def get_sentiment_sources():
    """
    API endpoint to get sentiment data sources.

    Query Parameters:
        type (str, optional): Filter sources by type (e.g., 'news', 'reddit')
        crypto (str, optional): Filter sources by mentioned cryptocurrency (e.g., 'BTC')
        limit (int, optional): Maximum number of sources to return
        days (int, optional): Time period for sentiment data (1, 7, or 30 days). Default is 1.

    Returns:
        JSON response containing sentiment sources.
    """
    try:
        # Get the requested time range (default to 1 day if not specified)
        days = request.args.get("days", default=1, type=int)

        # Validate days parameter (only allow 1, 7, or 30 days)
        if days not in [1, 7, 30]:
            days = 1

        # If days=1, just return the latest data
        if days == 1:
            sentiment_data = db.get_latest_sentiment()
            if sentiment_data is None:
                return (
                    jsonify(
                        {
                            "error": "No sentiment data available",
                            "message": "Sentiment data is being collected. Please try again later.",
                        }
                    ),
                    404,
                )
            sources = sentiment_data.get("sources", [])
        else:
            # For longer time ranges, collect sources from all snapshots in range
            cutoff = datetime.now() - timedelta(days=days)
            snapshots = db.get_sentiment_snapshots_since(cutoff)

            if not snapshots:
                sentiment_data = db.get_latest_sentiment()
                sources = (
                    sentiment_data.get("sources", []) if sentiment_data else []
                )
            else:
                all_sources = []
                for _created_at, data in snapshots:
                    all_sources.extend(data.get("sources", []))

                # Remove duplicates (based on URL, falling back to title)
                unique_sources = {}
                for source in all_sources:
                    identifier = source.get("url", "") or source.get("title", "")
                    if identifier and identifier not in unique_sources:
                        unique_sources[identifier] = source

                sources = list(unique_sources.values())

        # Filter by source type if specified in query parameters
        source_type = request.args.get("type")
        if source_type:
            sources = [s for s in sources if s.get("type") == source_type]

        # Filter by cryptocurrency if specified in query parameters
        crypto = request.args.get("crypto")
        if crypto:
            sources = [
                s for s in sources if crypto.upper() in s.get("mentioned_cryptos", [])
            ]

        # Sort sources by timestamp (newest first)
        sources = sorted(sources, key=lambda x: x.get("timestamp", ""), reverse=True)

        # Limit the number of sources if specified in query parameters
        limit = request.args.get("limit", type=int)
        if limit and limit > 0:
            sources = sources[:limit]

        return jsonify({"sources": sources, "count": len(sources)})
    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


@app.route("/api/sentiment/trends", methods=["GET"])
def get_sentiment_trends():
    """
    API endpoint to get sentiment trends over time.

    Query Parameters:
        days (int, optional): Time period for trend data (1, 7, or 30 days). Default is 7.
        symbol (str, optional): Cryptocurrency symbol to get trends for. Default is "BTC".
                               Use "all" for overall market sentiment.

    Returns:
        JSON response containing sentiment and price trends over time.
    """
    try:
        # Get the requested time range from query parameters
        days = request.args.get("days", default=7, type=int)

        # Validate days parameter (only allow 1, 7, or 30 days)
        if days not in [1, 7, 30]:
            days = 7

        # Get the cryptocurrency symbol from query parameters
        symbol = request.args.get("symbol", default="BTC")

        # Get all sentiment snapshots within the requested time range (oldest first)
        cutoff = datetime.now() - timedelta(days=days)
        snapshots = db.get_sentiment_snapshots_since(cutoff)

        # Initialize trend data structure
        trend_data = {
            "dates": [],
            "sentiment": [],
            "price": [],
        }

        # Extract sentiment data from each snapshot
        for created_at, data in snapshots:
            try:
                # Add date to trend data
                trend_data["dates"].append(created_at.strftime("%Y-%m-%d %H:%M"))

                # Add overall sentiment or crypto-specific sentiment based on the symbol
                if symbol == "all" or symbol == "BTC":
                    # Use overall market sentiment
                    sentiment_value = data.get("overall", {}).get("sentiment", 50)
                else:
                    # Get crypto-specific sentiment if available
                    crypto_data = data.get("crypto_specific", {}).get(
                        symbol.upper(), {}
                    )
                    if crypto_data and crypto_data.get("sentiment_scores"):
                        sentiment_value = sum(crypto_data["sentiment_scores"]) / len(
                            crypto_data["sentiment_scores"]
                        )
                    else:
                        sentiment_value = 50  # Default if no data available

                trend_data["sentiment"].append(sentiment_value)

                # Get real price data from the historical price data
                # For 'all', use BTC as the market indicator
                price_symbol = "btc" if symbol == "all" else symbol.lower()

                # The timestamp for this data point
                timestamp = created_at

                # Find the closest price data point to this timestamp
                price = get_closest_price_for_timestamp(price_symbol, timestamp)

                # If no price data found, try to get it from the next available data point
                if price is None:
                    price = get_latest_price_before_timestamp(price_symbol, timestamp)

                    if price is None:
                        price = get_earliest_price_after_timestamp(
                            price_symbol, timestamp
                        )

                        # If we still don't have price data, use the latest available price
                        if price is None:
                            latest_data = get_latest_data()
                            for crypto in latest_data:
                                if crypto["symbol"] == price_symbol:
                                    price = crypto["price"]
                                    break

                trend_data["price"].append(price)

            except Exception:
                continue

        return jsonify(trend_data)
    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Top gainers API
# ---------------------------------------------------------------------------


@app.route("/api/top-gainers", methods=["GET"])
def get_top_gainers_data():
    """
    API endpoint to get the top gaining cryptocurrencies.

    Query Parameters:
        limit (int, optional): Maximum number of top gainers to return. Default is 20.

    Returns:
        JSON response containing top gainer cryptocurrency data.
    """
    try:
        # Get optional limit parameter
        limit = request.args.get("limit", default=20, type=int)

        # Get the most recent top gainers snapshot from the database
        created_at, top_gainers_data = db.get_latest_top_gainers()

        # If there is no snapshot yet, scrape the data now and store it
        if not top_gainers_data:
            gainers_scraper = TopGainersScraper()
            data_processor = DataProcessor()
            top_gainers = gainers_scraper.get_top_gainers(limit)
            if top_gainers:
                data_processor.save_top_gainers_data(top_gainers)
                created_at, top_gainers_data = db.get_latest_top_gainers()
            else:
                return (
                    jsonify(
                        {
                            "error": "No top gainers data available",
                            "message": "Failed to retrieve top gainers data. Please try again later.",
                        }
                    ),
                    404,
                )

        # Limit the number of results if requested
        if limit and limit > 0 and limit < len(top_gainers_data):
            top_gainers_data = top_gainers_data[:limit]

        # Use the snapshot's creation time as the last-updated marker
        last_updated = (
            created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else None
        )

        return jsonify({"data": top_gainers_data, "last_updated": last_updated})

    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


@app.route("/api/top-gainers/update", methods=["GET"])
def trigger_top_gainers_update():
    """
    API endpoint to manually trigger a top gainers data update.

    Query Parameters:
        force (bool, optional): Force update even if data is recent. Default is false.

    Returns:
        JSON response indicating whether the update was triggered or skipped.
    """
    try:
        # Get optional force parameter (default: false)
        force_update = request.args.get("force", "false").lower() == "true"

        # Check whether the latest snapshot is recent enough to skip an update
        created_at, _ = db.get_latest_top_gainers()
        if created_at is not None and not force_update:
            file_age = (datetime.now() - created_at).total_seconds()

            # If snapshot is less than 15 minutes old, don't update unless forced
            if file_age < 15 * 60:
                return jsonify(
                    {
                        "message": f"Top gainers data is recent (updated {int(file_age/60)} minutes ago). Use force=true to update anyway.",
                        "status": "skipped",
                    }
                )

        # Create a new thread to update top gainers data
        def update_top_gainers():
            gainers_scraper = TopGainersScraper()
            data_processor = DataProcessor()
            top_gainers = gainers_scraper.get_top_gainers()
            if top_gainers:
                data_processor.save_top_gainers_data(top_gainers)

        update_thread = threading.Thread(target=update_top_gainers)
        update_thread.daemon = True
        update_thread.start()

        return jsonify(
            {"message": "Top gainers data update triggered", "status": "success"}
        )
    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


@app.route("/api/sentiment/update", methods=["GET"])
def trigger_sentiment_update():
    """
    API endpoint to manually trigger a sentiment data update.

    Query Parameters:
        force (bool, optional): Force update even if data is recent. Default is false.

    Returns:
        JSON response indicating whether the update was triggered or skipped.
    """
    try:
        # Get optional force parameter (default: false)
        force_update = request.args.get("force", "false").lower() == "true"

        # Check whether the latest snapshot is recent enough to skip an update
        latest_time = db.get_latest_sentiment_time()
        if latest_time is not None and not force_update:
            file_age = (datetime.now() - latest_time).total_seconds()

            # If snapshot is less than 30 minutes old, don't update unless forced
            if file_age < 30 * 60:
                return jsonify(
                    {
                        "message": f"Sentiment data is recent (updated {int(file_age/60)} minutes ago). Use force=true to update anyway.",
                        "status": "skipped",
                    }
                )

        # Create a new thread to update sentiment data and clean up old snapshots
        def update_and_cleanup():
            try:
                scraper = SentimentScraper()
                scraper.run_scraper()
                # Clean up old sentiment snapshots (older than 30 days)
                scraper.cleanup_old_sentiment_data(30)
            except Exception:
                pass

        update_thread = threading.Thread(target=update_and_cleanup)
        update_thread.daemon = True
        update_thread.start()

        return jsonify(
            {"message": "Sentiment data update triggered", "status": "success"}
        )
    except Exception as e:
        return jsonify({"error": "Server error", "message": str(e)}), 500


if __name__ == "__main__":
    """
    Main application entry point.

    When the script is run directly, this section:
    1. Ensures the database schema exists
    2. Starts background threads for periodic data updates
    3. Launches the Flask web server with configuration from environment variables
    """
    # Ensure database tables exist
    db.init_db()

    # Start background thread for cryptocurrency price data updates
    price_updater = threading.Thread(target=update_data_periodically)
    price_updater.daemon = True  # Thread will exit when main program exits
    price_updater.start()

    # Start background thread for sentiment data updates
    sentiment_updater = threading.Thread(target=update_sentiment_data_periodically)
    sentiment_updater.daemon = True  # Thread will exit when main program exits
    sentiment_updater.start()

    # Start background thread for top gainers data updates
    top_gainers_updater = threading.Thread(target=update_top_gainers_periodically)
    top_gainers_updater.daemon = True  # Thread will exit when main program exits
    top_gainers_updater.start()

    # Start the Flask web server
    app.run(
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("FLASK_PORT", 5000)),
    )
