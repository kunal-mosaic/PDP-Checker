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

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  |  %(message)s",
        datefmt="%H:%M:%S"
    ))

    # File
    log_file = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  |  %(message)s"
    ))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
