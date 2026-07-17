"""Central configuration for Nexora File Store.

All values come from environment variables / secrets — never hardcode
credentials in source. See replit.md for the required variable names.
"""
from __future__ import annotations

import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it via Replit secrets before starting the bot."
        )
    return value


class Settings:
    # my.telegram.org application credentials — required by the MTProto
    # client library (Kurigram) for every bot, including the main bot and
    # every clone.
    api_id: int = int(_require("TELEGRAM_API_ID"))
    api_hash: str = _require("TELEGRAM_API_HASH")

    # Nexora File Store main bot token (from @BotFather).
    bot_token: str = _require("TELEGRAM_BOT_TOKEN")

    # Neon Postgres connection string, e.g.
    # postgresql://user:pass@host/db?sslmode=require
    database_url: str = _require("NEON_DATABASE_URL")

    # Telegram numeric user id of the person who administers Nexora itself.
    # Defaults to the known owner ID; can be overridden via env var.
    main_owner_id: int = int(os.environ.get("MAIN_BOT_OWNER_ID", "8186068163"))

    # Channel/group id where Nexora posts its own operational logs
    # (new owner, new clone created, clone deleted, errors).
    main_log_channel_id: int = int(os.environ.get("MAIN_LOG_CHANNEL_ID", "0"))


settings = Settings()
