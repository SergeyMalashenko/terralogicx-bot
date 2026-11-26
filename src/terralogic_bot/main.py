"""Точка входа: конфигурация → logging → aiogram polling."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from .client import McpTerraLogicClient
from .config import Settings
from .handlers import ReportService, build_router
from .legaldocs import load_offer_text, resolve_offer_version
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
    # действующая редакция оферты — в БД: условия заказов восстанавливаются
    offer_text = load_offer_text(settings)
    if offer_text is not None:
        quota_store.register_offer_version(
            resolve_offer_version(settings), offer_text
        )
    else:
        logging.getLogger("terralogic_bot").warning(
            "offer text not readable: %s — версия не зарегистрирована",
            settings.offer_text_path,
        )
    report_service = ReportService(client, settings, quota_store)
    dispatcher = Dispatcher()
    # поддержка первой: перехватывает сообщения активных сессий и группы
    dispatcher.include_router(
        build_support_router(settings, support_store, quota_store)
    )
    dispatcher.include_router(
        build_router(client, settings, quota_store, report_service=report_service)
    )
    bot = Bot(settings.telegram_bot_token, session=session)
    sweeper = asyncio.create_task(
        support_sweeper(bot, support_store, settings.support_session_ttl_s)
    )
    webhook_runner = None
    if settings.webhook_enabled:
        from .handlers import PAY_PAID_TEXT, BotChatSender, fulfill_paid_order
        from .webhook import build_webhook_app, start_webhook

        async def notify_paid(payment: Mapping[str, Any]) -> None:
            user_id = int(payment["user_id"])  # type: ignore[arg-type]
            fulfilled = await fulfill_paid_order(
                payment=payment,
                service=report_service,
                sender=BotChatSender(bot, user_id),
            )
            if not fulfilled:  # заказ без предмета — старая схема кредитов
                await bot.send_message(
                    user_id,
                    PAY_PAID_TEXT.format(reports=int(payment["reports"])),  # type: ignore[arg-type]
                )

        webhook_runner = await start_webhook(
            build_webhook_app(
                quota=quota_store,
                password2=settings.robokassa_password2 or "",
                notify=notify_paid,
            ),
            host=settings.robokassa_webhook_host,
            port=settings.robokassa_webhook_port,
        )
    try:
        await dispatcher.start_polling(bot)
    finally:
        if webhook_runner is not None:
            await webhook_runner.cleanup()
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
