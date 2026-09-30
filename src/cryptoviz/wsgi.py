"""
WSGI entry point.

Gunicorn loads this module and serves the module-level `app`:

    gunicorn cryptoviz.wsgi:app
"""

from .app import create_app

app = create_app()
