"""Start Foundry Sentinel.

    python run_sentinel.py

Works from any folder: it finds the `sentinel` package and the `.env` file
next to this script.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)  # so .env and sentinel.db are found next to this file

if not (HERE / "sentinel" / "app.py").exists():
    sys.exit(f"Can't find the 'sentinel' folder next to {Path(__file__).name}. "
             "Keep this file in the foundry-sentinel project folder.")
if not (HERE / ".env").exists() and not os.environ.get("SENTINEL_UPSTREAM_URL"):
    sys.exit("No .env file found. Copy .env.example to .env and fill in SENTINEL_UPSTREAM_URL "
             "and SENTINEL_UPSTREAM_API_KEY.")

try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
    import uvicorn
except ImportError as e:
    sys.exit(f"Missing package ({e.name}). Run:  pip install -r requirements.txt")

import logging

from sentinel.app import create_app
from sentinel.config import _load_dotenv

_load_dotenv(os.environ.get("SENTINEL_ENV_FILE", ".env"))

logging.basicConfig(level=os.environ.get("SENTINEL_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    port = int(os.environ.get("SENTINEL_PORT", "8080"))
    print(f"Foundry Sentinel starting on http://localhost:{port}  (dashboard: /sentinel)")
    uvicorn.run(create_app(), host=os.environ.get("SENTINEL_HOST", "0.0.0.0"), port=port, log_level="info")
