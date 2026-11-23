"""
One-off historical price backfill.

Fetches price history from CoinGecko for the current top 10 coins and writes it
into the crypto_prices table, so that volatility and correlation have data to
work with before the live scraper has accumulated any. Points are stored at the
times CoinGecko reports them, which is hourly for these ranges.

CoinGecko rate-limits aggressively on the free tier, so requests are chunked by
week and deliberately paced; a full run takes several minutes. Each coin is saved
as soon as it is fetched, and re-running only updates rows that already exist.

Run it with:  uv run cryptoviz-backfill
"""

import logging
import time
from datetime import UTC, datetime, timedelta

import pandas as pd
import requests

from ..db import engine as db_engine
from ..db import repository as db
from ..db.models import TIMESTAMP_FORMAT
from .base import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

# How far back a backfill reaches unless told otherwise.
DEFAULT_BACKFILL_DAYS = 31

MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
RANGE_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"

REQUEST_TIMEOUT_SECONDS = 15
CHUNK_DAYS = 7
SECONDS_BETWEEN_CHUNKS = 10
SECONDS_BETWEEN_COINS = 15
REQUEST_ATTEMPTS = 3

# Wait after a failed request, and the most a Retry-After header can ask for.
RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 120

# Used only when the current top 10 cannot be fetched.
FALLBACK_COINS = (
    ("bitcoin", "btc", "Bitcoin"),
    ("ethereum", "eth", "Ethereum"),
    ("tether", "usdt", "Tether"),
    ("ripple", "xrp", "XRP"),
    ("binancecoin", "bnb", "BNB"),
    ("solana", "sol", "Solana"),
    ("usd-coin", "usdc", "USDC"),
    ("dogecoin", "doge", "Dogecoin"),
    ("tron", "trx", "TRON"),
    ("cardano", "ada", "Cardano"),
)


def _retry_delay(response):
    """Seconds to wait after a 429, honoring Retry-After up to a limit."""
    try:
        seconds = int(response.headers.get("Retry-After", RETRY_SECONDS))
    except TypeError, ValueError:
        seconds = RETRY_SECONDS
    return min(max(seconds, 1), MAX_RETRY_SECONDS)


def _request_json(url, params):
    """
    GET a JSON document, retrying rate limits and transient failures.

    Returns:
        dict | list | None: The parsed body, or None when every attempt failed.
    """
    headers = {"User-Agent": DEFAULT_USER_AGENT}

    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        delay = RETRY_SECONDS
        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
            )
            if response.status_code == 429:
                delay = _retry_delay(response)
                logger.warning("Rate limited by CoinGecko; waiting %ds", delay)
            else:
                response.raise_for_status()
                return response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("CoinGecko request failed: %s", exc)

        if attempt < REQUEST_ATTEMPTS:
            time.sleep(delay)

    return None


def _tracked_coins():
    """Return (coin id, symbol, name) for the current top 10 by market cap."""
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 10,
        "page": 1,
        "sparkline": False,
    }
    markets = _request_json(MARKETS_URL, params)
    if not markets:
        logger.warning("Could not fetch the top 10; using the built-in coin list")
        return FALLBACK_COINS
    return [(c["id"], c["symbol"].lower(), c["name"]) for c in markets]


def _build_rows(symbol, name, prices, market_caps, volumes):
    """
    Turn CoinGecko's parallel series into price rows.

    Args:
        symbol (str): Lowercase coin symbol.
        name (str): Display name.
        prices, market_caps, volumes (list): [millisecond timestamp, value] pairs.

    Returns:
        list[dict]: Rows with UTC timestamps, USD amounts, and a 24h change that
        is None when no point exists about a day earlier.
    """
    if not prices:
        return []

    frame = pd.DataFrame(prices, columns=["ms", "price"]).drop_duplicates("ms")
    for column, series in (("market_cap", market_caps), ("volume_24h", volumes)):
        values = pd.DataFrame(series, columns=["ms", column]).drop_duplicates("ms")
        frame = frame.merge(values, on="ms", how="left")

    frame = frame.sort_values("ms")
    frame["time"] = pd.to_datetime(frame["ms"], unit="ms").astype("datetime64[ns]")
    frame["day_ago"] = frame["time"] - pd.Timedelta(days=1)

    earlier = frame[["time", "price"]].rename(
        columns={"time": "earlier_time", "price": "earlier_price"}
    )
    frame = pd.merge_asof(
        frame.sort_values("day_ago"),
        earlier,
        left_on="day_ago",
        right_on="earlier_time",
        direction="nearest",
        tolerance=pd.Timedelta(minutes=30),
    )
    frame["change"] = (frame["price"] / frame["earlier_price"] - 1) * 100

    return [
        {
            "symbol": symbol,
            "name": name,
            "price": row.price,
            "market_cap": row.market_cap,
            "volume_24h": row.volume_24h,
            "percent_change_24h": round(row.change, 2) if pd.notna(row.change) else None,
            "timestamp": row.time.strftime(TIMESTAMP_FORMAT),
        }
        for row in frame.itertuples()
    ]


def _fetch_coin(coin_id, symbol, name, start, end):
    """Fetch one coin's history in weekly chunks and build its rows."""
    prices, market_caps, volumes = [], [], []
    url = RANGE_URL.format(coin_id=coin_id)

    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end)
        logger.info(
            "Fetching %s: %s to %s",
            coin_id,
            chunk_start.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
        )

        params = {
            "vs_currency": "usd",
            "from": int(chunk_start.timestamp()),
            "to": int(chunk_end.timestamp()),
        }
        data = _request_json(url, params)
        if data is None:
            logger.error(
                "Gap in %s history: no data from %s to %s",
                coin_id,
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
            )
        else:
            prices.extend(data.get("prices", []))
            market_caps.extend(data.get("market_caps", []))
            volumes.extend(data.get("total_volumes", []))

        chunk_start = chunk_end
        if chunk_start < end:
            time.sleep(SECONDS_BETWEEN_CHUNKS)

    return _build_rows(symbol, name, prices, market_caps, volumes)


def main(days=None):
    """Fetch the historical window and persist it to the database."""
    days = days or DEFAULT_BACKFILL_DAYS
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(days=days)

    db_engine.init_db()

    stored = 0
    for index, (coin_id, symbol, name) in enumerate(_tracked_coins()):
        if index:
            time.sleep(SECONDS_BETWEEN_COINS)

        rows = _fetch_coin(coin_id, symbol, name, start, end)
        if not rows:
            logger.warning("No price data available for %s", coin_id)
            continue

        written = db.save_prices(rows)
        stored += written
        logger.info("Saved %d rows for %s", written, coin_id)

    if not stored:
        logger.error("No historical data collected; nothing was written")
        return 1

    # The backfill inserts history behind the current time, which the
    # worker's short trailing refresh would never reach - rebuild in full.
    logger.info("Rebuilding price rollups over the backfilled range")
    written = db.refresh_price_buckets()
    logger.info("Saved %d historical rows; rollup buckets written: %s", stored, written)
    return 0
