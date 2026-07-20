"""
Configuration for StickerBot.
Loads BOT_TOKEN from environment variable or .env file.
"""

from __future__ import annotations

import os
from pathlib import Path

# Project root (directory containing this file)
ROOT_DIR = Path(__file__).resolve().parent

# Load .env if present
_ENV_FILE = ROOT_DIR / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\ufeff")  # strip BOM if present
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value

# Required
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")

# Optional — AI providers (all free, all optional)
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
HF_API_KEY: str = os.environ.get("HF_API_KEY", "")

# Deployment mode
# Render sets PORT automatically; if present → webhook mode, else → polling
RENDER: bool = "RENDER" in os.environ or "PORT" in os.environ
PORT: int = int(os.environ.get("PORT", "8080"))
WEBHOOK_URL: str = os.environ.get("WEBHOOK_URL", "")

# Optional
DB_PATH: str = os.environ.get("DB_PATH", str(ROOT_DIR / "stickerbot.db"))
INLINE_CACHE_TIME: int = int(os.environ.get("INLINE_CACHE_TIME", "1"))
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# Validate on import
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set. Create a .env file with:\nBOT_TOKEN=your_token_here"
    )
