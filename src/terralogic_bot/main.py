"""Точка входа: конфигурация → logging → aiogram polling."""

from __future__ import annotations

import asyncio
import logging

from .client import McpTerraLogicClient
from .config import Settings
from .handlers import build_router


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
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(client, settings))
    bot = Bot(settings.telegram_bot_token, session=session)
    await dispatcher.start_polling(bot)


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
