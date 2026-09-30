"""
Logging setup shared by the web process, the worker and the CLI entry points.

Output goes to stdout with no file handling or rotation of its own: under
systemd that is captured by the journal, which already handles rotation and
retention.
"""

import logging
import sys

from . import config

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level=None):
    """
    Configure root logging once for the current process.

    Args:
        level (str, optional): Log level name. Defaults to the configured level.
    """
    resolved = (level or config.LOG_LEVEL).upper()

    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format=LOG_FORMAT,
        stream=sys.stdout,
        force=True,
    )

    # These libraries log every request/connection at INFO, which drowns out the
    # application's own messages.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
