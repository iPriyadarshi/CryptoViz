"""
Flask application factory.

The app serves the frontend and the JSON API from one origin. It does *not*
start any scraping: that belongs to the separate worker process
(cryptoviz.worker), because gunicorn runs several web workers and each would
otherwise start its own duplicate set of scrape loops.
"""

import logging

from flask import Flask
from flask_cors import CORS

from . import config
from .api import ALL_BLUEPRINTS
from .db import init_db
from .logging_config import configure_logging

logger = logging.getLogger(__name__)


def create_app():
    """
    Build and configure the Flask application.

    Returns:
        flask.Flask: The configured application.
    """
    configure_logging()

    # static_url_path="" serves everything under static/ at the site root, so
    # the frontend's absolute references (/css/..., /js/...) resolve unchanged.
    app = Flask(
        __name__,
        static_folder=str(config.PACKAGE_ROOT / "static"),
        static_url_path="",
        template_folder=str(config.PACKAGE_ROOT / "templates"),
    )

    app.config["JSON_SORT_KEYS"] = False

    # The frontend is same-origin; this only opens the API to other consumers.
    CORS(app, resources={r"/api/*": {"origins": config.CORS_ORIGINS}})

    for blueprint in ALL_BLUEPRINTS:
        app.register_blueprint(blueprint)

    # Create the schema up front so a fresh machine serves requests without a
    # manual migration step.
    try:
        init_db()
    except Exception:
        logger.exception("Could not initialize the database on startup")

    logger.info("CryptoViz application initialized")
    return app
