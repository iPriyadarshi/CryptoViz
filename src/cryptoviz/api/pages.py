"""
Frontend page routes.

The HTML pages live in templates/ and are served by this blueprint; the static
assets they reference are served by Flask's static handler mounted at the site
root (in production nginx serves them directly and never reaches Flask).
"""

from flask import Blueprint, render_template

bp = Blueprint("pages", __name__)

# The pages that make up the frontend, each served at /<name>.html.
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


@bp.route("/")
def home():
    """Serve the main dashboard."""
    return render_template("index.html")


@bp.route("/api-docs")
def api_docs():
    """Serve the API documentation page."""
    return render_template("api_docs.html")


@bp.route("/healthz")
def healthz():
    """
    Liveness probe.

    Deliberately does not touch the database: this answers "is the web process
    up", which is what a load balancer or systemd watchdog needs to know.
    """
    return {"status": "ok"}


def _make_page_view(template_name):
    """Build a view function that renders one specific template."""

    def view():
        return render_template(template_name)

    return view


# Register an explicit route per page. Explicit rules take precedence over the
# static-file handler, so /volatility.html resolves to the template.
for _page in FRONTEND_PAGES:
    bp.add_url_rule(
        f"/{_page}.html",
        endpoint=f"page_{_page}",
        view_func=_make_page_view(f"{_page}.html"),
    )
