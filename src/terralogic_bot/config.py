"""Конфигурация бота из переменных окружения (без секретов в коде)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    terralogic_mcp_url: str
    request_timeout_s: float
    max_concurrent_reports: int
    log_level: str
    telegram_proxy: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        token = (source.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN не задан — получите токен у @BotFather"
            )
        return cls(
            telegram_bot_token=token,
            terralogic_mcp_url=source.get(
                "TERRALOGIC_MCP_URL", "http://127.0.0.1:8004/mcp"
            ),
            request_timeout_s=float(
                source.get("TERRALOGIC_REQUEST_TIMEOUT_S", "900")
            ),
            max_concurrent_reports=int(source.get("MAX_CONCURRENT_REPORTS", "2")),
            log_level=source.get("LOG_LEVEL", "INFO"),
            telegram_proxy=(source.get("TELEGRAM_PROXY") or "").strip() or None,
        )
