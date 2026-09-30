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
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .. import config
from ..db import repository as db

logger = logging.getLogger(__name__)

# Assets whose price is pegged, so correlation against them is meaningless.
STABLECOINS = ("usdt", "usdc")

# Below this coefficient of variation an asset is treated as pegged even if it
# is not in the list above.
STABLECOIN_CV_THRESHOLD = 0.001

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

    cutoff = datetime.now() - timedelta(days=days)
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


def calculate_volatility(symbol, days=7):
    """
    Calculate volatility metrics for one cryptocurrency over a time window.

    Args:
        symbol (str): The cryptocurrency symbol (e.g. 'btc').
        days (int, optional): Size of the window in days.

    Returns:
        dict: {'volatility', 'max_drawdown', 'daily_returns', 'timestamps'}.
        Zeros and empty lists when there is not enough data.
    """
    empty = {
        "volatility": 0,
        "max_drawdown": 0,
        "daily_returns": [],
        "timestamps": [],
    }

    cutoff = datetime.now() - timedelta(days=days)
    df = db.get_prices_df(symbol, since=cutoff, bucket=bucket_for_window(days))
    if df.empty or len(df) < 2:
        return empty

    df = df.sort_values("timestamp")

    # Period-over-period returns between consecutive observations.
    df["daily_return"] = df["price"].pct_change() * 100
    df = df.dropna(subset=["daily_return"])
    if df.empty:
        return empty

    volatility = float(np.std(df["daily_return"]))

    # Maximum drawdown: the largest drop from a running peak.
    running_max = df["price"].cummax()
    drawdown = (df["price"] - running_max) / running_max * 100
    max_drawdown = float(drawdown.min())

    return {
        "volatility": round(volatility, 2),
        "max_drawdown": round(max_drawdown, 2),
        "daily_returns": df["daily_return"].tolist(),
        "timestamps": df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
    }


def get_all_volatility(days=7):
    """
    Get volatility metrics for every tracked cryptocurrency.

    Args:
        days (int, optional): Size of the window in days.

    Returns:
        list[dict]: One entry per cryptocurrency, sorted by volatility
        descending. Empty when there is no data.
    """
    cutoff = datetime.now() - timedelta(days=days)
    df = db.get_prices_df(since=cutoff, bucket=bucket_for_window(days))
    if df.empty:
        return []

    results = []
    for symbol, group in df.groupby("symbol"):
        try:
            group = group.sort_values("timestamp")
            latest = group.iloc[-1]

            # Derive the metrics from the slice already in memory rather than
            # issuing another query per symbol.
            returns = group["price"].pct_change().dropna() * 100
            if len(returns) >= 1:
                volatility = round(float(np.std(returns)), 2)
                running_max = group["price"].cummax()
                drawdown = (group["price"] - running_max) / running_max * 100
                max_drawdown = round(float(drawdown.min()), 2)
            else:
                volatility = 0.0
                max_drawdown = 0.0

            results.append(
                {
                    "symbol": symbol,
                    "name": latest["name"],
                    "price": float(latest["price"]),
                    "volatility": volatility,
                    "max_drawdown": max_drawdown,
                    "percent_change_24h": float(latest["percent_change_24h"] or 0),
                }
            )
        except Exception as exc:
            logger.warning("Volatility calculation failed for %s: %s", symbol, exc)
            continue

    return sorted(results, key=lambda item: item["volatility"], reverse=True)


def calculate_correlation_matrix(days=7):
    """
    Calculate the price-return correlation matrix across cryptocurrencies.

    Correlation is computed on returns rather than prices, so it reflects
    co-movement instead of shared trend. Stablecoins and any asset whose price
    barely varies are excluded, since a near-constant series produces a
    meaningless (or undefined) correlation.

    Args:
        days (int, optional): Size of the window in days.

    Returns:
        dict: {'correlation_matrix': [[...]], 'symbols': [...]}, both empty when
        there is not enough data.
    """
    empty = {"correlation_matrix": [], "symbols": []}

    try:
        cutoff = datetime.now() - timedelta(days=days)
        df = db.get_prices_df(since=cutoff, bucket=bucket_for_window(days))
        if df.empty or len(df) < 2:
            return empty

        # One column per symbol, one row per observation time.
        pivot = df.pivot_table(
            index="timestamp", columns="symbol", values="price", aggfunc="mean"
        )

        pivot = pivot.drop(
            columns=[c for c in STABLECOINS if c in pivot.columns], errors="ignore"
        )

        # Keep symbols that have enough observations and enough price variation.
        valid_symbols = []
        for symbol in pivot.columns:
            series = pivot[symbol]
            if series.count() < 2:
                continue

            mean = series.mean()
            cv = 0 if not mean else series.std() / mean
            if cv < STABLECOIN_CV_THRESHOLD:
                logger.debug("Skipping %s: price variation too low (cv=%.6f)", symbol, cv)
                continue

            valid_symbols.append(symbol)

        if len(valid_symbols) < 2:
            logger.info("Not enough symbols with usable price variation for correlation")
            return empty

        pivot = pivot[valid_symbols]

        # Forward-fill gaps so a missing observation for one asset does not drop
        # the whole timestamp, then take returns.
        returns = pivot.ffill().pct_change(fill_method=None).dropna()
        if len(returns) < 2 or len(returns.columns) < 2:
            logger.info("Not enough return observations for correlation")
            return empty

        corr = returns.corr().round(2)

        # NaN is not valid JSON; the API contract uses null for an undefined
        # correlation pair.
        matrix = [
            [None if pd.isna(value) else float(value) for value in row]
            for row in corr.to_numpy()
        ]

        return {"correlation_matrix": matrix, "symbols": corr.columns.tolist()}

    except Exception as exc:
        logger.error("Correlation calculation failed: %s", exc)
        return empty


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


def cleanup_old_data():
    """
    Remove price data older than the configured retention window.

    Prunes the rollup buckets alongside the raw rows, so a bucket never outlives
    the data it summarizes.
    """
    cutoff = datetime.now() - timedelta(days=config.PRICE_RETENTION_DAYS)
    removed = db.delete_prices_before(cutoff)
    removed_buckets = db.delete_price_buckets_before(cutoff)
    if removed or removed_buckets:
        logger.info(
            "Removed %d price rows and %d rollup buckets past the retention window",
            removed,
            removed_buckets,
        )
    return removed
