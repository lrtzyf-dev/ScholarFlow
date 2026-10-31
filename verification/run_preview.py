"""Run a local offline preview without changing the user's regular service."""
import os
import sys
import argparse
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=8766)
parser.add_argument('--data-dir', type=Path, default=root / 'verification' / 'runtime')
args = parser.parse_args()
os.environ.update({
    "DATA_DIR": str(args.data_dir.resolve()),
    "LLM_API_KEY": "",
    "EMBEDDING_API_KEY": "",
    "REDIS_URL": "",
    "DESKTOP_NOTIFICATIONS": "false",
    "MOCK_STREAM_DELAY": "0.02",
})
sys.path.insert(0, str(root / "backend"))

from app.config import settings  # noqa: E402
import uvicorn  # noqa: E402

# Saved preview settings may override environment defaults on startup.
settings.llm_api_key = ""
settings.embedding_api_key = ""
settings.redis_url = ""
settings.desktop_notifications = False

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)
