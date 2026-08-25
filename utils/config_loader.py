import os
import yaml
from dotenv import load_dotenv
from pathlib import Path

ROOT = Path(__file__).parent.parent
load_dotenv(dotenv_path=ROOT / ".env", override=True)


def load_config() -> dict:
    config_path = ROOT / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_env(key: str, required: bool = True, default: str = "") -> str:
    val = os.getenv(key) or default
    if required and not val:
        raise EnvironmentError(
            f"Missing required environment variable: {key}\n"
            f"Add it to your .env file. See .env.example for reference."
        )
    return val
