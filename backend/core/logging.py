import logging
from logging.handlers import RotatingFileHandler

from backend.core.config import ROOT


def configure_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    logger = logging.getLogger("research")
    if not logger.handlers:
        handler = RotatingFileHandler(
            ROOT / "logs/app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
