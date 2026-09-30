"""
CryptoViz - cryptocurrency analytics dashboard.

The package is organized in layers, each depending only on the ones below it:

    api/        Flask blueprints (HTTP concerns only)
    services/   analytics over stored data, and the background collection loops
    scrapers/   external data sources, each behind a class returning plain dicts
    db/         schema, engine and all SQL
    config.py   every environment variable, read in one place

Two processes run in production: the web server (cryptoviz.wsgi:app) and the
scraper worker (cryptoviz.worker), sharing one database.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
