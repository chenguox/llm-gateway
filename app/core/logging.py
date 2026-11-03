"""Console logging configuration."""

import logging


def configure_logging(level: str) -> None:
    """Initialize console output, preserving handlers installed by the host."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("app").setLevel(level)
