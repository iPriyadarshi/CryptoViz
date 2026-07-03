"""
Cryptocurrency Data Utilities Module

This module provides utility functions for processing, storing, and analyzing
cryptocurrency data. It includes functions for saving price data, retrieving
historical data, calculating volatility metrics, and computing correlation matrices.

Persistence is backed by the database layer (see database.py) instead of a CSV
file. The module maintains a rolling window of historical data, automatically
removing data older than the configured retention period.
"""

import pandas as pd
import time
import numpy as np
from datetime import datetime, timedelta

import database as db
from sentiment_scraper import SentimentScraper
from scraper import (
    CryptoDataScraper,
    TopGainersScraper,
    DataProcessor,
)


# Configuration constants
MAX_DAYS_TO_KEEP = 31  # Number of days to retain historical data (1 month)
UPDATE_INTERVAL = (
    300  # 5 minutes in seconds - interval for updating cryptocurrency price data
)
SENTIMENT_UPDATE_INTERVAL = (
    300  # 5 minutes in seconds - interval for updating sentiment data
)
TOP_GAINERS_UPDATE_INTERVAL = (
    300  # 5 minutes in seconds - interval for updating top gainers data
)


def save_price_data(data):
    """
    Save cryptocurrency price data to the database.

    Args:
        data (list): List of dictionaries containing cryptocurrency data

    Note:
        After saving, this function automatically calls cleanup_old_data() to
        remove data older than MAX_DAYS_TO_KEEP.
    """
    if not data:
        return

    # Persist the batch of price rows (timestamps are normalized in the DB layer)
    db.save_prices(data)

    # Clean up old data after saving new data
    cleanup_old_data()


def get_latest_data():
    """
    Get the latest cryptocurrency data from the database.

    Returns:
        list: A list of dictionaries containing the latest data for each cryptocurrency,
              or an empty list if there is no data.
    """
    df = db.get_prices_df()
    if df.empty:
        return []

    # Get the most recent timestamp
    latest_timestamp = df["timestamp"].max()

    # Filter for the latest data (rows with the most recent timestamp)
    latest_data = df[df["timestamp"] == latest_timestamp]

    # Convert DataFrame to list of dictionaries
    return latest_data.to_dict("records")


def get_history(symbol):
    """
    Get historical price data for a specific cryptocurrency.

    This function retrieves all historical price data for the specified cryptocurrency
    within the retention period (MAX_DAYS_TO_KEEP).

    Args:
        symbol (str): The cryptocurrency symbol (e.g., 'btc', 'eth')

    Returns:
        dict: A dictionary containing lists of prices and timestamps:
              {
                  'prices': [50000.0, 51000.0, ...],
                  'timestamps': ["2023-01-01 12:00:00", ...]
              }
              Returns empty lists if no data is found.
    """
    df = db.get_prices_df(symbol)
    if df.empty:
        return {"prices": [], "timestamps": []}

    # Filter for data within the retention period
    cutoff = datetime.now() - timedelta(days=MAX_DAYS_TO_KEEP)
    filtered = df[df["timestamp"] >= cutoff]

    # Sort by timestamp (oldest to newest)
    filtered = filtered.sort_values("timestamp")

    if len(filtered) == 0:
        return {"prices": [], "timestamps": []}

    # Remove duplicate consecutive prices to avoid flat lines in the chart
    # This creates a new column 'price_changed' that is True when the price is different from the previous row
    filtered["price_changed"] = filtered["price"].diff().ne(0)

    # Always keep the first row (set its price_changed to True)
    if len(filtered) > 0:
        filtered.iloc[0, filtered.columns.get_loc("price_changed")] = True

    # Keep rows where either the price changed or at least 5 minutes have passed since the last kept row
    kept_indices = []
    last_kept_time = None

    for idx, row in filtered.iterrows():
        current_time = row["timestamp"]
        price_changed = row["price_changed"]

        # Always keep the first row
        if last_kept_time is None:
            kept_indices.append(idx)
            last_kept_time = current_time
            continue

        # Keep if price changed or 5 minutes have passed
        time_diff = (current_time - last_kept_time).total_seconds()
        if price_changed or time_diff >= 300:  # 300 seconds = 5 minutes
            kept_indices.append(idx)
            last_kept_time = current_time

    # Filter to only keep the selected rows
    filtered = filtered.loc[kept_indices]

    # Return prices and timestamps as lists
    return {
        "prices": filtered["price"].tolist(),
        "timestamps": filtered["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
    }


def cleanup_old_data():
    """
    Remove data older than the configured retention period.

    Note:
        This function is automatically called after saving new data to
        maintain the rolling window of historical data.
    """
    cutoff = datetime.now() - timedelta(days=MAX_DAYS_TO_KEEP)
    removed = db.delete_prices_before(cutoff)
    if removed:
        print(f"Removed {removed} old records")


def calculate_volatility(symbol, days=7):
    """
    Calculate volatility metrics for a specific cryptocurrency over a given time period.

    This function calculates several volatility metrics including standard deviation
    of daily returns and maximum drawdown for the specified cryptocurrency.

    Args:
        symbol (str): The cryptocurrency symbol (e.g., 'btc', 'eth')
        days (int, optional): Number of days to calculate volatility for. Defaults to 7.

    Returns:
        dict: Dictionary containing volatility metrics:
              {
                  'volatility': 5.2,           # Standard deviation of daily returns
                  'max_drawdown': -12.5,       # Maximum percentage drop from peak
                  'daily_returns': [2.1, -1.3, ...],  # List of daily percentage returns
                  'timestamps': ["2023-01-01 12:00:00", ...]  # Corresponding timestamps
              }
              Returns zeros and empty lists if insufficient data is available
    """
    df = db.get_prices_df(symbol)
    if df.empty:
        return {
            "volatility": 0,
            "max_drawdown": 0,
            "daily_returns": [],
            "timestamps": [],
        }

    # Filter for the time period
    cutoff = datetime.now() - timedelta(days=days)
    filtered = df[df["timestamp"] >= cutoff]

    # Sort by timestamp (oldest to newest)
    filtered = filtered.sort_values("timestamp")

    # Check if we have enough data points for calculation
    if len(filtered) < 2:
        return {
            "volatility": 0,
            "max_drawdown": 0,
            "daily_returns": [],
            "timestamps": [],
        }

    # Calculate daily returns (percentage change between consecutive prices)
    filtered["prev_price"] = filtered["price"].shift(1)
    filtered["daily_return"] = (
        (filtered["price"] - filtered["prev_price"]) / filtered["prev_price"] * 100
    )
    filtered = filtered.dropna()  # Remove rows with NaN values (first row after shift)

    # Calculate volatility (standard deviation of daily returns)
    volatility = np.std(filtered["daily_return"])

    # Calculate maximum drawdown (largest percentage drop from a peak)
    filtered["cummax"] = filtered["price"].cummax()  # Running maximum price
    filtered["drawdown"] = (
        (filtered["price"] - filtered["cummax"]) / filtered["cummax"] * 100
    )
    max_drawdown = filtered["drawdown"].min()

    # Return the calculated metrics
    return {
        "volatility": round(volatility, 2),  # Round to 2 decimal places
        "max_drawdown": round(max_drawdown, 2),  # Round to 2 decimal places
        "daily_returns": filtered["daily_return"].tolist(),
        "timestamps": filtered["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
    }


def get_all_volatility(days=7):
    """
    Get volatility data for all cryptocurrencies in the dataset.

    This function calculates volatility metrics for all cryptocurrencies
    in the dataset and returns them sorted by volatility (highest first).

    Args:
        days (int, optional): Number of days to calculate volatility for. Defaults to 7.

    Returns:
        list: List of dictionaries containing volatility data for each cryptocurrency.
              Returns an empty list if no data is available.
    """
    try:
        df = db.get_prices_df()
        if df.empty:
            return []

        # Get unique cryptocurrency symbols
        symbols = df["symbol"].unique()

        # Calculate volatility for each cryptocurrency
        result = []
        for symbol in symbols:
            try:
                # Calculate volatility metrics
                volatility_data = calculate_volatility(symbol, days)

                # Check if there's data for this symbol
                symbol_data = df[df["symbol"] == symbol]
                if len(symbol_data) == 0:
                    continue  # Skip if no data for this symbol

                # Get the latest data point for this symbol
                latest_data = symbol_data.sort_values(
                    "timestamp", ascending=False
                ).iloc[0]

                # Add to results
                result.append(
                    {
                        "symbol": symbol,
                        "name": latest_data["name"],
                        "price": float(latest_data["price"]),
                        "volatility": float(volatility_data["volatility"]),
                        "max_drawdown": float(volatility_data["max_drawdown"]),
                        "percent_change_24h": float(latest_data["percent_change_24h"]),
                    }
                )
            except Exception as e:
                print(f"Error calculating volatility for {symbol}: {str(e)}")
                # Add with default values if there's an error but we have basic data
                try:
                    symbol_data = df[df["symbol"] == symbol]
                    if len(symbol_data) > 0:
                        latest_data = symbol_data.sort_values(
                            "timestamp", ascending=False
                        ).iloc[0]
                        result.append(
                            {
                                "symbol": symbol,
                                "name": latest_data["name"],
                                "price": float(latest_data["price"]),
                                "volatility": 0.0,  # Default value for volatility
                                "max_drawdown": 0.0,  # Default value for max_drawdown
                                "percent_change_24h": float(
                                    latest_data["percent_change_24h"]
                                ),
                            }
                        )
                except:
                    pass  # Skip if we can't even get basic data

        # Sort by volatility (descending)
        result = sorted(result, key=lambda x: x["volatility"], reverse=True)

        return result
    except Exception as e:
        print(f"Error in get_all_volatility: {str(e)}")
        return []  # Return empty list on error


def calculate_correlation_matrix(days=7):
    """
    Calculate price correlation matrix between cryptocurrencies.

    This function analyzes how the price movements of different cryptocurrencies
    correlate with each other over the specified time period. A correlation of 1.0
    means perfect positive correlation, -1.0 means perfect negative correlation,
    and 0 means no correlation.

    The function automatically excludes stablecoins and cryptocurrencies with
    insufficient price variation to provide meaningful correlation data.

    Args:
        days (int, optional): Number of days to calculate correlation for. Defaults to 7.

    Returns:
        dict: Dictionary containing correlation matrix and symbols:
              {
                  'correlation_matrix': [[1.0, 0.75, ...], ...],
                  'symbols': ['btc', 'eth', ...]
              }
              Returns empty lists if insufficient data is available
    """
    try:
        df = db.get_prices_df()
        if df.empty:
            return {"correlation_matrix": [], "symbols": []}
        print(f"Data loaded, shape: {df.shape}")

        # Filter for data within the specified time period
        cutoff = datetime.now() - timedelta(days=days)
        print(f"Filtering data from {cutoff} to now")
        filtered = df[df["timestamp"] >= cutoff]
        print(f"Filtered data shape: {filtered.shape}")

        # Check if we have enough data points
        if len(filtered) < 2:
            print("Not enough data for correlation calculation")
            return {"correlation_matrix": [], "symbols": []}

        # Get unique symbols, excluding stablecoins (which have minimal price variation)
        stablecoins = ["usdt", "usdc"]  # List of stablecoins to exclude
        symbols = [s for s in filtered["symbol"].unique() if s not in stablecoins]
        print(f"Unique symbols found (excluding stablecoins): {len(symbols)}")
        print(f"Symbols: {symbols}")

        # Create a pivot table with timestamps as index and symbols as columns
        # This reshapes the data to have one row per timestamp and one column per cryptocurrency
        print("Creating pivot table...")
        pivot = filtered.pivot_table(
            index="timestamp", columns="symbol", values="price", aggfunc="mean"
        )
        print(f"Pivot table shape: {pivot.shape}")
        print(f"Pivot table first few rows:\n{pivot.head()}")

        # Filter out stablecoins from pivot table for cleaner correlation analysis
        for stablecoin in stablecoins:
            if stablecoin in pivot.columns:
                print(f"Removing stablecoin {stablecoin} from pivot table")
                pivot = pivot.drop(columns=[stablecoin])

        # Check if we have enough data points for each symbol
        # We need at least 2 data points to calculate returns
        valid_symbols = []
        for symbol in pivot.columns:
            # Count non-NaN values for this symbol
            count = pivot[symbol].count()
            print(f"Symbol {symbol} has {count} data points")

            # Check if the symbol has price variation (not a stablecoin)
            price_std = pivot[symbol].std()  # Standard deviation of price
            price_mean = pivot[symbol].mean()  # Mean price

            # Calculate coefficient of variation (CV) to identify low-volatility assets
            # CV = standard deviation / mean (normalized measure of dispersion)
            cv = 0 if price_mean == 0 else price_std / price_mean
            print(f"Symbol {symbol} has coefficient of variation: {cv:.6f}")

            # Skip symbols with very low variation (likely stablecoins or pegged assets)
            if cv < 0.001:  # Threshold for identifying stablecoins
                print(
                    f"Skipping {symbol} due to very low price variation (likely a stablecoin)"
                )
                continue

            # Only include symbols with at least 2 data points
            if count >= 2:
                valid_symbols.append(symbol)

        print(f"Valid symbols with enough data points: {len(valid_symbols)}")
        if len(valid_symbols) < 2:
            print("Not enough valid symbols with sufficient data points")
            return {"correlation_matrix": [], "symbols": []}

        # Filter pivot table to include only valid symbols
        pivot = pivot[valid_symbols]
        print(f"Filtered pivot table shape: {pivot.shape}")

        # Calculate daily returns (percentage change between consecutive prices)
        print("Calculating daily returns...")
        # Fill NaN values using ffill() (forward fill) as recommended by the warning
        pivot_filled = pivot.ffill()
        # Use fill_method=None as recommended by the warning
        returns = pivot_filled.pct_change(fill_method=None)
        print(f"Returns data before dropna:\n{returns.head()}")

        # Drop rows with NaN values (first row will have NaN after pct_change)
        returns = returns.dropna()
        print(f"Returns data after dropna, shape: {returns.shape}")

        # Check if we have enough data after calculating returns
        if len(returns) < 1:
            print("Not enough data after calculating returns")
            return {"correlation_matrix": [], "symbols": []}

        # Check if we have at least 2 rows for correlation calculation
        if len(returns) < 2:
            print("Need at least 2 rows of return data for correlation calculation")
            return {"correlation_matrix": [], "symbols": []}

        # Check if we have at least 2 columns (symbols) for correlation calculation
        if len(returns.columns) < 2:
            print(
                f"Need at least 2 symbols for correlation calculation, but only have {len(returns.columns)}"
            )
            return {"correlation_matrix": [], "symbols": []}

        # Calculate correlation matrix and round to 2 decimal places
        print("Calculating correlation matrix...")
        corr_matrix = returns.corr().round(2)
        print(f"Correlation matrix shape: {corr_matrix.shape}")

        # Replace NaN values with None for proper JSON serialization
        print("Replacing NaN values with None...")
        corr_matrix = corr_matrix.where(corr_matrix.notna(), None)

        # Additional check to ensure no NaN values remain
        corr_matrix = corr_matrix.replace({np.nan: None})

        # Convert to list format for JSON serialization
        print("Converting to list format for JSON serialization...")
        matrix_list = []
        for row in corr_matrix.values:
            row_list = []
            for val in row:
                if isinstance(val, float) and np.isnan(val):
                    row_list.append(None)
                else:
                    row_list.append(val)
            matrix_list.append(row_list)

        # Get the list of symbols in the correlation matrix
        symbols_list = corr_matrix.columns.tolist()

        # Prepare the final result
        result = {"correlation_matrix": matrix_list, "symbols": symbols_list}

        print(
            f"Final result: {len(symbols_list)} symbols, {len(matrix_list)}x{len(matrix_list[0]) if matrix_list else 0} matrix"
        )
        return result
    except Exception as e:
        print(f"Error in calculate_correlation_matrix: {str(e)}")
        return {"correlation_matrix": [], "symbols": []}  # Return empty data on error


# Helper functions for getting price data from the database
def get_closest_price_for_timestamp(symbol, timestamp):
    """
    Get the price data point closest to the given timestamp for a specific cryptocurrency.

    Args:
        symbol (str): The cryptocurrency symbol (e.g., 'btc', 'eth')
        timestamp (datetime): The target timestamp

    Returns:
        float: The price at the closest timestamp, or None if no data is found
    """
    try:
        filtered = db.get_prices_df(symbol)

        if filtered.empty:
            return None  # No data for this symbol

        # Calculate time difference between each data point and the target timestamp
        filtered["time_diff"] = abs(
            (filtered["timestamp"] - timestamp).dt.total_seconds()
        )

        # Find the row with the smallest time difference
        closest_row = filtered.loc[filtered["time_diff"].idxmin()]

        return closest_row["price"]

    except Exception:
        return None


def get_latest_price_before_timestamp(symbol, timestamp):
    """
    Get the latest price data point before the given timestamp for a specific cryptocurrency.

    Args:
        symbol (str): The cryptocurrency symbol (e.g., 'btc', 'eth')
        timestamp (datetime): The target timestamp

    Returns:
        float: The price at the latest timestamp before the target, or None if no data is found
    """
    try:
        df = db.get_prices_df(symbol)
        if df.empty:
            return None

        # Filter for timestamps before the target
        filtered = df[df["timestamp"] <= timestamp]

        if filtered.empty:
            return None  # No data for this symbol before the timestamp

        # Get the latest data point before the timestamp
        latest_row = filtered.sort_values("timestamp", ascending=False).iloc[0]

        return latest_row["price"]

    except Exception:
        return None


def get_earliest_price_after_timestamp(symbol, timestamp):
    """
    Get the earliest price data point after the given timestamp for a specific cryptocurrency.

    Args:
        symbol (str): The cryptocurrency symbol (e.g., 'btc', 'eth')
        timestamp (datetime): The target timestamp

    Returns:
        float: The price at the earliest timestamp after the target, or None if no data is found
    """
    try:
        df = db.get_prices_df(symbol)
        if df.empty:
            return None

        # Filter for timestamps after the target
        filtered = df[df["timestamp"] >= timestamp]

        if filtered.empty:
            return None  # No data for this symbol after the timestamp

        # Get the earliest data point after the timestamp
        earliest_row = filtered.sort_values("timestamp").iloc[0]

        return earliest_row["price"]

    except Exception:
        return None


def update_data_periodically():
    """
    Background task to update cryptocurrency price data at regular intervals.

    This function runs in a separate thread and continuously:
    1. Scrapes current cryptocurrency price data from external sources
    2. Saves the data to the database for historical tracking
    3. Sleeps for the configured UPDATE_INTERVAL before repeating

    The function handles exceptions to ensure the background thread doesn't crash.
    """
    # Create instance of the scraper
    crypto_scraper = CryptoDataScraper()

    while True:
        try:
            # Scrape latest cryptocurrency data from external sources
            crypto_data = crypto_scraper.get_crypto_data()

            # If data was successfully retrieved, save it to the database
            if crypto_data:
                save_price_data(crypto_data)

        except Exception:
            pass
        finally:
            # Wait for the configured interval before the next update
            time.sleep(UPDATE_INTERVAL)


def update_sentiment_data_periodically():
    """
    Background task to update market sentiment data at regular intervals.

    This function runs in a separate thread and continuously:
    1. Creates a sentiment scraper instance
    2. Collects sentiment data from various sources (news, social media, etc.)
    3. Processes and saves the sentiment data
    4. Sleeps for the configured SENTIMENT_UPDATE_INTERVAL before repeating

    The function handles exceptions to ensure the background thread doesn't crash.
    """
    while True:
        try:
            # Create sentiment scraper instance
            scraper = SentimentScraper()

            # Run the scraper to collect and process sentiment data
            scraper.run_scraper()

            # Clean up old sentiment data (older than 30 days)
            try:
                scraper.cleanup_old_sentiment_data(30)
            except Exception:
                pass

        except Exception:
            pass
        finally:
            # Wait for the configured interval before the next update
            time.sleep(SENTIMENT_UPDATE_INTERVAL)


def update_top_gainers_periodically():
    """
    Background task to update top gainers data at regular intervals.

    This function runs in a separate thread and continuously:
    1. Scrapes current top gaining cryptocurrencies from external sources
    2. Saves the data to the database for historical tracking
    3. Cleans up old data (older than 24 hours)
    4. Sleeps for the configured TOP_GAINERS_UPDATE_INTERVAL before repeating

    The function handles exceptions to ensure the background thread doesn't crash.
    """
    # Create instances of the scraper and data processor
    gainers_scraper = TopGainersScraper()
    data_processor = DataProcessor()

    while True:
        try:
            # Scrape top gainers data
            top_gainers_data = gainers_scraper.get_top_gainers()

            # If data was successfully retrieved, save it
            if top_gainers_data:
                data_processor.save_top_gainers_data(top_gainers_data)

                # Clean up old data
                data_processor.cleanup_old_top_gainers_data(24)  # Remove data older than 24 hours

        except Exception:
            pass
        finally:
            # Wait for the configured interval before the next update
            time.sleep(TOP_GAINERS_UPDATE_INTERVAL)
