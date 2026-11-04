"""Run with:  python -m sentinel  (reads SENTINEL_* env vars / .env)"""
import logging
import os

import uvicorn

from .app import create_app
from .config import _load_dotenv

_load_dotenv(os.environ.get("SENTINEL_ENV_FILE", ".env"))

logging.basicConfig(level=os.environ.get("SENTINEL_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.environ.get("SENTINEL_HOST", "0.0.0.0"),
                port=int(os.environ.get("SENTINEL_PORT", "8080")), log_level="info")
