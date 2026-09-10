import os
from pathlib import Path


class Config:
    """Runtime locations and defaults for the offline platform."""

    HOST = "127.0.0.1"
    DEFAULT_PORT = 8765
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    DATABASE_PATH = Path(os.environ.get("PLATFORM_DATABASE_PATH", PROJECT_ROOT / "data" / "platform.db"))
    LOG_PATH = Path(os.environ.get("PLATFORM_LOG_PATH", PROJECT_ROOT / "logs"))
    VERSION = "2.0.0"
