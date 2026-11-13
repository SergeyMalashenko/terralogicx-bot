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
    support_group_id: int | None = None
    support_session_ttl_s: float = 3600.0
    support_db_path: str = "data/support.sqlite3"
    report_limit: int = 5
    unlimited_user_ids: frozenset[int] = frozenset()
    quota_db_path: str = "data/quota.sqlite3"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        if env is None:
            from dotenv import load_dotenv

            load_dotenv()  # .env рядом с запуском; реальные env-переменные важнее
        source = os.environ if env is None else env
        token = (source.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN не задан — получите токен у @BotFather"
            )
        raw_group = (source.get("SUPPORT_GROUP_ID") or "").strip()
        support_group_id: int | None = None
        if raw_group:
            try:
                support_group_id = int(raw_group)
            except ValueError as exc:
                raise RuntimeError(
                    "SUPPORT_GROUP_ID должен быть целым числом "
                    "(id группы, например -1001234567890)"
                ) from exc
        unlimited_user_ids: set[int] = set()
        raw_unlimited = (source.get("UNLIMITED_USER_IDS") or "").strip()
        if raw_unlimited:
            try:
                unlimited_user_ids = {
                    int(item) for item in raw_unlimited.split(",") if item.strip()
                }
            except ValueError as exc:
                raise RuntimeError(
                    "UNLIMITED_USER_IDS должен быть списком id через запятую, "
                    "например 123456789,987654321"
                ) from exc
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
            support_group_id=support_group_id,
            support_session_ttl_s=float(
                source.get("SUPPORT_SESSION_TTL_S", "3600")
            ),
            support_db_path=source.get(
                "SUPPORT_DB_PATH", "data/support.sqlite3"
            ),
            report_limit=int(source.get("REPORT_LIMIT", "5")),
            unlimited_user_ids=frozenset(unlimited_user_ids),
            quota_db_path=source.get("QUOTA_DB_PATH", "data/quota.sqlite3"),
        )
