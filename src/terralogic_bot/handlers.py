"""Telegram handlers: /start, /help и приём кадастрового номера.

Статические тексты (без LLM). Дорогая операция формирования отчёта
выполняется фоновой задачей с дедупликацией (user_id, КН) и ограничением
параллелизма (asyncio.Semaphore). Пользователю не показываются traceback.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from time import monotonic

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

from .cadastral import parse_cadastral_number
from .client import TerraLogicClient
from .config import Settings
from .errors import (
    BotError,
    ParcelNotFound,
    SourceUnavailable,
    TerraLogicUnavailable,
)

LOGGER = logging.getLogger("terralogic_bot")

START_TEXT = (
    "Здравствуйте!\n\n"
    "Отправьте кадастровый номер земельного участка, "
    "и я сформирую экспресс-оценку TerraLogicX.\n\n"
    "Например:\n50:03:0070305:370"
)

HELP_TEXT = (
    "Отправьте кадастровый номер текстовым сообщением — "
    "я сформирую и пришлю экспресс-оценку участка в PDF.\n\n"
    "Формат номера: 50:03:0070305:370"
)

INVALID_TEXT = (
    "Не удалось распознать кадастровый номер.\n\n"
    "Проверьте номер и отправьте его ещё раз.\n\n"
    "Например:\n50:03:0070305:370"
)

DUPLICATE_TEXT = "Экспресс-оценка этого участка уже формируется."

NOT_FOUND_TEXT = (
    "Не удалось найти участок с кадастровым номером\n"
    "{cadastral_number}.\n\n"
    "Проверьте номер и попробуйте ещё раз."
)

UNAVAILABLE_TEXT = (
    "Сейчас не удалось сформировать отчёт из-за временной "
    "недоступности одного из сервисов.\n\n"
    "Попробуйте повторить запрос позже."
)

FAILED_TEXT = (
    "Не удалось сформировать отчёт по этому участку.\n\n"
    "Попробуйте повторить запрос позже."
)


def report_filename(cadastral_number: str) -> str:
    """Безопасное предсказуемое имя PDF: TerraLogicX_50-21-0030210-123.pdf."""
    return f"TerraLogicX_{cadastral_number.replace(':', '-')}.pdf"


async def _report_job(
    *,
    message: Message,
    cadastral_number: str,
    client: TerraLogicClient,
    semaphore: asyncio.Semaphore,
    jobs: dict[tuple[int, str], asyncio.Task],
    key: tuple[int, str],
    request_id: str,
) -> None:
    """Фоновая задача: существующий use case TerraLogicX → PDF → Telegram."""
    started = monotonic()
    user_id = key[0]
    try:
        async with semaphore:
            result = await client.prepare_quickreport(cadastral_number)
        filename = report_filename(cadastral_number)
        await message.answer_document(
            BufferedInputFile(result.pdf_bytes, filename=filename),
            caption=f"Экспресс-оценка участка\n{cadastral_number}",
        )
        LOGGER.info(
            "%s done: user=%s kn=%s case=%s run=%s %.1fs",
            request_id,
            user_id,
            cadastral_number,
            result.case_id,
            result.collection_run_id,
            monotonic() - started,
        )
    except ParcelNotFound:
        await message.answer(
            NOT_FOUND_TEXT.format(cadastral_number=cadastral_number)
        )
        LOGGER.info("%s not found: user=%s kn=%s", request_id, user_id, cadastral_number)
    except (SourceUnavailable, TerraLogicUnavailable) as exc:
        await message.answer(UNAVAILABLE_TEXT)
        LOGGER.warning(
            "%s unavailable: user=%s kn=%s error=%s",
            request_id, user_id, cadastral_number, exc,
        )
    except BotError as exc:
        await message.answer(FAILED_TEXT)
        LOGGER.warning(
            "%s failed: user=%s kn=%s error=%s: %s",
            request_id, user_id, cadastral_number, type(exc).__name__, exc,
        )
    except Exception:  # последний рубеж, traceback только в лог
        await message.answer(FAILED_TEXT)
        LOGGER.exception(
            "%s unexpected: user=%s kn=%s", request_id, user_id, cadastral_number
        )
    finally:
        jobs.pop(key, None)


def build_router(client: TerraLogicClient, settings: Settings) -> Router:
    """Router с замкнутыми зависимостями: client, лимиты, active jobs."""
    router = Router(name="terralogic-bot")
    semaphore = asyncio.Semaphore(settings.max_concurrent_reports)
    jobs: dict[tuple[int, str], asyncio.Task] = {}

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(START_TEXT)

    @router.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(HELP_TEXT)

    @router.message(F.text)
    async def cadastral(message: Message) -> None:
        cadastral_number = parse_cadastral_number(message.text or "")
        if cadastral_number is None:
            await message.answer(INVALID_TEXT)
            return
        user_id = message.from_user.id if message.from_user else 0
        key = (user_id, cadastral_number)
        if key in jobs:
            await message.answer(DUPLICATE_TEXT)
            return
        request_id = uuid.uuid4().hex[:8]
        LOGGER.info(
            "%s accepted: user=%s kn=%s", request_id, user_id, cadastral_number
        )
        await message.answer(
            f"Принял участок {cadastral_number}.\n\nФормирую экспресс-оценку."
        )
        task = asyncio.create_task(
            _report_job(
                message=message,
                cadastral_number=cadastral_number,
                client=client,
                semaphore=semaphore,
                jobs=jobs,
                key=key,
                request_id=request_id,
            )
        )
        jobs[key] = task

    return router
