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
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message

from .cadastral import parse_cadastral_number
from .client import TerraLogicClient
from .config import Settings
from .errors import (
    BotError,
    CaseNotCollected,
    ParcelNotFound,
    SourceUnavailable,
    TerraLogicUnavailable,
)
from .models import CollectionStatus

LOGGER = logging.getLogger("terralogic_bot")

START_TEXT = (
    "🏡 TerraLogicX — анализ земельных участков для строительства\n\n"
    "Бот помогает предварительно оценить возможность строительства "
    "жилого дома на земельном участке в Московской области.\n\n"
    "📋 Что содержит PDF-отчёт:\n"
    "• Назначение и характеристики земельного участка.\n"
    "• Выявленные ограничения на строительство.\n"
    "• Предварительную оценку строительного потенциала.\n"
    "• Рекомендации по дополнительным проверкам.\n\n"
    "🚀 Как получить отчёт:\n\n"
    "Отправьте кадастровый номер участка, например 50:29:0030502:979.\n\n"
    "Дождитесь завершения анализа.\n\n"
    "Получите PDF-отчёт с результатами.\n\n"
    "ℹ️ Важно: Анализ проводится на основе доступных данных и не является "
    "официальным подтверждением возможности строительства. При недостатке "
    "существенных сведений итоговая оценка может отсутствовать."
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

STATUS_USAGE_TEXT = (
    "Использование: /status 50:29:0030502:979\n\n"
    "Команда показывает, что удалось и что не получилось собрать "
    "по уже обработанному участку."
)

NOT_COLLECTED_TEXT = (
    "По участку {cadastral_number} ещё нет собранных данных.\n\n"
    "Отправьте кадастровый номер текстовым сообщением — сформирую отчёт."
)

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


_SOURCE_LABELS = {
    "nspd": "НСПД",
    "rgis": "РГИС Московской области",
    "osm": "OpenStreetMap",
    "dgis": "2GIS",
    "documents_sync": "Реестр документов",
    "geodocs": "Скачивание и извлечение документов",
}
_SOURCE_ICONS = {
    "ok": "✅",
    "partial": "⚠️",
    "failed": "❌",
    "skipped": "➖",
    "not_applicable": "➖",
}
_DOC_TYPE_LABELS = {"pzz": "ПЗЗ", "general_plan": "Генплан"}
_DOC_STATUS_LABELS = {
    "extracted": ("✅", "данные извлечены"),
    "downloaded": ("✅", "скачан"),
    "pending": ("⏳", "зарегистрирован, ожидает загрузки"),
    "not_found": ("❌", "не найден"),
    "failed": ("❌", "ошибка загрузки"),
}
_COLLECTION_STATUS_LABELS = {
    "complete": "собрано полностью",
    "partial": "собрано частично",
    "failed": "сбор не завершён",
}


def _short(text: str, limit: int = 120) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def render_collection_status(
    cadastral_number: str, status: CollectionStatus
) -> str:
    """Статический текст сводки сбора для /status (без LLM)."""
    header_status = _COLLECTION_STATUS_LABELS.get(
        status.collection_status, status.collection_status
    )
    parts = [f"Сводка сбора: {cadastral_number}", f"Статус: {header_status}"]
    if status.reused_collection:
        parts[1] += " · данные из кэша"
    if status.duration_s is not None:
        parts[1] += f" · {status.duration_s:.0f} с"

    if status.sources:
        parts += ["", "Источники:"]
        for entry in status.sources:
            icon = _SOURCE_ICONS.get(entry.status, "❔")
            label = _SOURCE_LABELS.get(entry.source, entry.source)
            line = f"{icon} {label}"
            if entry.status == "skipped":
                line += " — не собирался"
            elif entry.status == "not_applicable":
                line += f" — неприменим{_short(', ' + entry.detail) if entry.detail else ''}"
            elif entry.detail:
                line += f" — {_short(entry.detail)}"
            parts.append(line)

    if status.documents:
        parts += ["", "Документы:"]
        for doc in status.documents:
            icon, state = _DOC_STATUS_LABELS.get(doc.status, ("❔", doc.status))
            label = _DOC_TYPE_LABELS.get(doc.doc_type or "", "Документ")
            if doc.number:
                label += f" № {doc.number}"
            if doc.version_date:
                label += f" от {doc.version_date}"
            line = f"{icon} {label} — {state}"
            if doc.reason:
                line += f" ({_short(doc.reason)})"
            parts.append(line)
    return "\n".join(parts)


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
    router = Router(name="terralogicx-bot")
    semaphore = asyncio.Semaphore(settings.max_concurrent_reports)
    jobs: dict[tuple[int, str], asyncio.Task] = {}

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(START_TEXT)

    @router.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(HELP_TEXT)

    @router.message(Command("status"))
    async def status(message: Message, command: CommandObject) -> None:
        cadastral_number = parse_cadastral_number(command.args or "")
        if cadastral_number is None:
            await message.answer(STATUS_USAGE_TEXT)
            return
        user_id = message.from_user.id if message.from_user else 0
        request_id = uuid.uuid4().hex[:8]
        try:
            summary = await client.get_collection_status(cadastral_number)
        except CaseNotCollected:
            await message.answer(
                NOT_COLLECTED_TEXT.format(cadastral_number=cadastral_number)
            )
            LOGGER.info(
                "%s status not collected: user=%s kn=%s",
                request_id, user_id, cadastral_number,
            )
            return
        except (SourceUnavailable, TerraLogicUnavailable) as exc:
            await message.answer(UNAVAILABLE_TEXT)
            LOGGER.warning(
                "%s status unavailable: user=%s kn=%s error=%s",
                request_id, user_id, cadastral_number, exc,
            )
            return
        except BotError as exc:
            await message.answer(FAILED_TEXT)
            LOGGER.warning(
                "%s status failed: user=%s kn=%s error=%s: %s",
                request_id, user_id, cadastral_number, type(exc).__name__, exc,
            )
            return
        await message.answer(render_collection_status(cadastral_number, summary))
        LOGGER.info(
            "%s status ok: user=%s kn=%s case=%s",
            request_id, user_id, cadastral_number, summary.case_id,
        )

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
