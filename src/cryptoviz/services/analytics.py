"""
Analytics over the stored price history.

Turns the raw crypto_prices rows into the derived series the API serves:
latest snapshot, per-symbol history, volatility metrics and the cross-asset
correlation matrix.

Two rules keep this affordable now that a full year of 5-minute data is kept:

  * every function filters by time in SQL, never by loading the table and
    filtering in pandas;
  * any window longer than a few days is downsampled in SQL before it is
    loaded, so a one-year request returns a few hundred points rather than the
    ~105,000 rows per symbol actually stored.
"""

import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from .. import config
from ..db import repository as db
from ..db.models import utc_now

logger = logging.getLogger(__name__)

# Assets whose price is pegged, so correlation against them is meaningless.
STABLECOINS = ("usdt", "usdc")

# An asset that never strays this far from $1 is treated as pegged even if it is
# not in the list above.
PEG_TOLERANCE = 0.02

# Fewest shared return observations a correlation pair needs to be reported.
MIN_CORRELATION_OBSERVATIONS = 5

# Minimum spacing between retained history points, in seconds. Consecutive rows
# with an unchanged price are dropped unless this much time has passed, which
# keeps flat stretches from bloating the chart payload.
HISTORY_MIN_SPACING_SECONDS = 300


def bucket_for_window(days):
    """
    Choose the SQL downsampling granularity for a window size.

    Args:
        days (int): Size of the requested window in days.

    Returns:
        str | None: 'day', 'hour', or None to use the raw 5-minute rows.
    """
    if days > config.DAILY_BUCKET_THRESHOLD_DAYS:
        return "day"
    if days > config.HOURLY_BUCKET_THRESHOLD_DAYS:
        return "hour"
    return None


def trend_bucket_for_window(days):
    """
    Choose the granularity for a sentiment trend chart.

    Args:
        days (int): Size of the requested window in days.

    Returns:
        str | None: 'day', 'hour', or None to plot every snapshot.
    """
    if days <= 1:
        return None
    return "hour" if days <= 14 else "day"


def get_latest_data():
    """
    Get the most recent price row for each tracked cryptocurrency.

    Returns:
        list[dict]: One record per cryptocurrency; empty when there is no data.
    """
    return db.get_latest_prices()


def get_history(symbol, days=None):
    """
    Get historical price data for one cryptocurrency.

    Consecutive points with an identical price are thinned out (keeping at least
    one point per HISTORY_MIN_SPACING_SECONDS) so the chart shows movement
    rather than a long flat line. Long windows are downsampled in SQL first.

    Args:
        symbol (str): The cryptocurrency symbol (e.g. 'btc').
        days (int, optional): Window size in days. Defaults to the full
            retention window.

    Returns:
        dict: {'prices': [...], 'timestamps': [...]}, both empty when there is
        no data for the symbol.
    """
    if days is None or days <= 0:
        days = config.PRICE_RETENTION_DAYS

    cutoff = utc_now() - timedelta(days=days)
    df = db.get_prices_df(symbol, since=cutoff, bucket=bucket_for_window(days))
    if df.empty:
        return {"prices": [], "timestamps": []}

    df = df.sort_values("timestamp").reset_index(drop=True)

    # Keep a row when its price differs from the previous kept row, or when
    # enough time has passed since the last kept row. The running "last kept"
    # state needs a sequential pass, so it runs over numpy arrays rather than
    # DataFrame rows.
    prices = df["price"].to_numpy()
    times = df["timestamp"].to_numpy()

    keep = np.zeros(len(df), dtype=bool)
    keep[0] = True
    last_kept_time = times[0]
    last_kept_price = prices[0]

    for i in range(1, len(df)):
        elapsed = (times[i] - last_kept_time) / np.timedelta64(1, "s")
        if prices[i] != last_kept_price or elapsed >= HISTORY_MIN_SPACING_SECONDS:
            keep[i] = True
            last_kept_time = times[i]
            last_kept_price = prices[i]

    kept = df[keep]

    return {
        "prices": kept["price"].tolist(),
        "timestamps": kept["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
    }


def _risk_metrics(series):
    """
    Calculate daily volatility and maximum drawdown for one sorted series.

    Volatility is realized volatility: squared log returns of the closing prices
    divided by the time they span, then expressed per day. Dividing by elapsed
    time keeps the figure comparable between 5-minute, hourly and daily data, and
    across gaps in collection.

    Drawdown compares each period's low against the highest price before it, and
    its close against its own high, so a crash inside a bucket is not averaged
    away.

    Args:
        series (pandas.DataFrame): rows with timestamp, close, low and high.

    Returns:
        tuple[float, float]: (daily volatility %, max drawdown %), each rounded
        to two decimals. Zeros when there are fewer than two points.
    """
    if len(series) < 2:
        return 0.0, 0.0

    log_returns = np.log(series["close"]).diff()
    elapsed_days = series["timestamp"].diff().dt.total_seconds() / 86400
    usable = log_returns.notna() & (elapsed_days > 0)
    total_days = elapsed_days[usable].sum()

    volatility = 0.0
    if total_days > 0:
        volatility = float(np.sqrt((log_returns[usable] ** 2).sum() / total_days) * 100)

    prior_peak = series["high"].cummax().shift(1)
    falls = pd.concat(
        [series["low"] / prior_peak - 1, series["close"] / series["high"] - 1], axis=1
    )
    max_drawdown = min(float(falls.min().min()) * 100, 0.0)

    return round(volatility, 2), round(max_drawdown, 2)


def calculate_volatility(symbol, days=7):
    """
    Calculate volatility metrics for one cryptocurrency over a time window.

    Args:
        symbol (str): The cryptocurrency symbol (e.g. 'btc').
        days (int, optional): Size of the window in days.

    Returns:
        dict: {'volatility', 'max_drawdown', 'daily_returns', 'timestamps'}.
        'volatility' is per day; 'daily_returns' holds the percentage return
        of each period in the series. Zeros and empty lists when there is not
        enough data.
    """
    empty = {
        "volatility": 0,
        "max_drawdown": 0,
        "daily_returns": [],
        "timestamps": [],
    }

    cutoff = utc_now() - timedelta(days=days)
    df = db.get_prices_df(symbol, since=cutoff, bucket=bucket_for_window(days))
    if df.empty or len(df) < 2:
        return empty

    df = df.sort_values("timestamp")
    volatility, max_drawdown = _risk_metrics(df)

    returns = df["close"].pct_change() * 100
    usable = returns.notna()

    return {
        "volatility": volatility,
        "max_drawdown": max_drawdown,
        "daily_returns": returns[usable].tolist(),
        "timestamps": df.loc[usable, "timestamp"]
        .dt.strftime("%Y-%m-%d %H:%M:%S")
        .tolist(),
    }


def get_all_volatility(days=7):
    """
    Get volatility metrics for every currently tracked cryptocurrency.

    Args:
        days (int, optional): Size of the window in days.

    Returns:
        list[dict]: One entry per cryptocurrency, sorted by volatility
        descending. Empty when there is no data.
    """
    cutoff = utc_now() - timedelta(days=days)
    df = db.get_prices_df(since=cutoff, bucket=bucket_for_window(days))
    if df.empty:
        return []

    # The headline price comes from the newest raw row, and a coin that has
    # left the top 10 is not listed with its stale price.
    latest_rows = {row["symbol"]: row for row in db.get_latest_prices()}

    results = []
    for symbol, group in df.groupby("symbol"):
        latest = latest_rows.get(symbol)
        if latest is None:
            continue

        try:
            volatility, max_drawdown = _risk_metrics(group.sort_values("timestamp"))

            results.append(
                {
                    "symbol": symbol,
                    "name": latest["name"],
                    "price": float(latest["price"]),
                    "volatility": volatility,
                    "max_drawdown": max_drawdown,
                    "percent_change_24h": latest["percent_change_24h"],
                }
            )
        except Exception as exc:
            logger.warning("Volatility calculation failed for %s: %s", symbol, exc)
            continue

    return sorted(results, key=lambda item: item["volatility"], reverse=True)


def _is_pegged(prices):
    """Whether a price series stays within PEG_TOLERANCE of $1."""
    return bool(((prices - 1).abs() <= PEG_TOLERANCE).all())


def calculate_correlation_matrix(days=7):
    """
    Calculate the price-return correlation matrix across cryptocurrencies.

    Correlation is computed on returns rather than prices, so it reflects
    co-movement instead of shared trend. Each pair uses every observation the
    two assets share, so a newly listed coin cannot shrink the sample for the
    others; a pair with too few shared observations is null. Pegged assets and
    series that never move are excluded, since their correlation is meaningless.

    Args:
        days (int, optional): Size of the window in days.

    Returns:
        dict: {'correlation_matrix': [[...]], 'symbols': [...]}, both empty when
        there is not enough data.
    """
    empty = {"correlation_matrix": [], "symbols": []}

    cutoff = utc_now() - timedelta(days=days)
    df = db.get_prices_df(since=cutoff, bucket=bucket_for_window(days))
    if df.empty or len(df) < 2:
        return empty

    # One column per symbol, one row per observation time.
    pivot = df.pivot_table(
        index="timestamp", columns="symbol", values="close", aggfunc="last"
    )
    pivot = pivot.drop(
        columns=[c for c in STABLECOINS if c in pivot.columns], errors="ignore"
    )

    # A gap stays a gap: filling it would invent a run of zero returns.
    returns = pivot.pct_change(fill_method=None)

    valid_symbols = [
        symbol
        for symbol in pivot.columns
        if returns[symbol].count() >= MIN_CORRELATION_OBSERVATIONS
        and returns[symbol].std() > 0
        and not _is_pegged(pivot[symbol].dropna())
    ]

    if len(valid_symbols) < 2:
        logger.info("Not enough symbols with usable price variation for correlation")
        return empty

    corr = returns[valid_symbols].corr(min_periods=MIN_CORRELATION_OBSERVATIONS).round(2)

    # NaN is not valid JSON; the API contract uses null for an undefined
    # correlation pair.
    matrix = [
        [None if pd.isna(value) else float(value) for value in row]
        for row in corr.to_numpy()
    ]

    return {"correlation_matrix": matrix, "symbols": corr.columns.tolist()}


# ---------------------------------------------------------------------------
# Point lookups used to align sentiment snapshots with prices
# ---------------------------------------------------------------------------


def get_price_series(symbol, since=None, bucket=None):
    """
    Load a symbol's (timestamp, price) series once for repeated lookups.

    The sentiment trends endpoint needs a price for every snapshot in a range;
    loading the series once and searching it in memory avoids a query per point.

    Args:
        symbol (str): The cryptocurrency symbol.
        since (datetime, optional): Restrict to rows at or after this time.
        bucket (str, optional): SQL downsampling granularity.

    Returns:
        pandas.DataFrame: sorted by timestamp, with 'timestamp' and 'price'
        columns. Empty when the symbol has no data.
    """
    df = db.get_prices_df(symbol, since=since, bucket=bucket)
    if df.empty:
        return df
    return df[["timestamp", "price"]].sort_values("timestamp").reset_index(drop=True)


def price_at(series, timestamp):
    """
    Find the price closest in time to a timestamp within a loaded series.

    Args:
        series (pandas.DataFrame): Output of get_price_series().
        timestamp (datetime): The target time.

    Returns:
        float | None: The nearest price, or None when the series is empty.
    """
    if series is None or series.empty:
        return None

    deltas = (series["timestamp"] - pd.Timestamp(timestamp)).abs()
    return float(series.loc[deltas.idxmin(), "price"])


def prices_at(series, timestamps):
    """
    Find the nearest price for each of many sorted timestamps at once.

    Args:
        series (pandas.DataFrame): Output of get_price_series().
        timestamps (list[datetime]): Target times, oldest first.

    Returns:
        list[float | None]: One price per timestamp; all None for an empty series.
    """
    if series is None or series.empty:
        return [None] * len(timestamps)

    targets = pd.DataFrame({"timestamp": pd.to_datetime(timestamps)})
    targets["timestamp"] = targets["timestamp"].astype("datetime64[ns]")
    prices = series.assign(timestamp=series["timestamp"].astype("datetime64[ns]"))

    merged = pd.merge_asof(targets, prices, on="timestamp", direction="nearest")
    return merged["price"].astype(float).tolist()


def cleanup_old_data():
    """
    Remove price data older than the configured retention window.

    Prunes the rollup buckets alongside the raw rows, so a bucket never outlives
    the data it summarizes.
    """
    cutoff = utc_now() - timedelta(days=config.PRICE_RETENTION_DAYS)
    removed = db.delete_prices_before(cutoff)
    removed_buckets = db.delete_price_buckets_before(cutoff)
    if removed or removed_buckets:
        logger.info(
            "Removed %d price rows and %d rollup buckets past the retention window",
            removed,
            removed_buckets,
        )
    return removed
