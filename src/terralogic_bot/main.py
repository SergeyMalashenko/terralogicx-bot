"""Точка входа: конфигурация → logging → aiogram polling."""

from __future__ import annotations

import asyncio
import logging

from .client import McpTerraLogicClient
from .config import Settings
from .handlers import build_router
from .quota import QuotaStore
from .support import SupportStore, build_support_router, support_sweeper


async def _run(settings: Settings) -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.aiohttp import AiohttpSession

    session = (
        AiohttpSession(proxy=settings.telegram_proxy)
        if settings.telegram_proxy
        else None
    )
    client = McpTerraLogicClient(
        settings.terralogic_mcp_url,
        read_timeout_s=settings.request_timeout_s,
    )
    support_store = SupportStore(settings.support_db_path)
    quota_store = QuotaStore(settings.quota_db_path)
    dispatcher = Dispatcher()
    # поддержка первой: перехватывает сообщения активных сессий и группы
    dispatcher.include_router(build_support_router(settings, support_store))
    dispatcher.include_router(build_router(client, settings, quota_store))
    bot = Bot(settings.telegram_bot_token, session=session)
    sweeper = asyncio.create_task(
        support_sweeper(bot, support_store, settings.support_session_ttl_s)
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        sweeper.cancel()
        support_store.close()
        quota_store.close()


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
