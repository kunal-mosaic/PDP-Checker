import logging
import sys
from pathlib import Path
from datetime import datetime

LOG_DIR = Path(__file__).parent.parent / "outputs"
LOG_DIR.mkdir(exist_ok=True)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    # Console — force UTF-8 so emoji/box-drawing chars don't crash on Windows
    # consoles (cp1252). reconfigure exists on Python 3.7+; ignore if unavailable.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  |  %(message)s",
        datefmt="%H:%M:%S"
    ))

    # File — explicit UTF-8 (Windows FileHandler defaults to cp1252 otherwise)
    log_file = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  |  %(message)s"
    ))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
