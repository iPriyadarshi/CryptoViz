"""
HTTP layer: one blueprint per area of the API, plus the frontend page routes.
"""

from .crypto import bp as crypto_bp
from .pages import bp as pages_bp
from .sentiment import bp as sentiment_bp
from .top_gainers import bp as top_gainers_bp

# Registered in this order by the application factory.
ALL_BLUEPRINTS = (pages_bp, crypto_bp, sentiment_bp, top_gainers_bp)

__all__ = [
    "ALL_BLUEPRINTS",
    "crypto_bp",
    "pages_bp",
    "sentiment_bp",
    "top_gainers_bp",
]
