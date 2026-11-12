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
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .cadastral import (
    extract_cadastral_number,
    parse_cadastral_number,
    parse_coordinates,
    parse_listing_url,
)
from .client import TerraLogicClient
from .config import Settings
from .errors import (
    BotError,
    CaseNotCollected,
    ListingResolutionFailed,
    ParcelNotFound,
    SourceUnavailable,
    TerraLogicUnavailable,
)
from .models import CollectionStatus, ListingResolution
from .support import support_start_button

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
    "Отправьте кадастровый номер участка, например 50:29:0030502:979, "
    "ссылку на объявление Авито или координаты участка с карты, "
    "например 55.488626, 38.009447.\n\n"
    "Дождитесь завершения анализа.\n\n"
    "Получите PDF-отчёт с результатами.\n\n"
    "ℹ️ Важно: Анализ проводится на основе доступных данных и не является "
    "официальным подтверждением возможности строительства. При недостатке "
    "существенных сведений итоговая оценка может отсутствовать."
)

HELP_TEXT = (
    "Отправьте кадастровый номер текстовым сообщением, ссылку на "
    "объявление Авито или координаты участка с карты — я сформирую "
    "и пришлю экспресс-оценку участка в PDF.\n\n"
    "Формат номера: 50:03:0070305:370\n"
    "Формат координат: 55.488626, 38.009447\n\n"
    "/support — связаться со службой поддержки."
)

INVALID_TEXT = (
    "Не удалось распознать кадастровый номер, ссылку на объявление "
    "или координаты.\n\n"
    "Отправьте номер, например:\n50:03:0070305:370\n\n"
    "ссылку на объявление Авито\n\n"
    "или координаты:\n55.488626, 38.009447"
)

LISTING_ACCEPTED_TEXT = (
    "Принял ссылку на объявление.\n\n"
    "Определяю участок по метке на карте и формирую экспресс-оценку."
)

COORDS_ACCEPTED_TEXT = (
    "Принял координаты.\n\n"
    "Определяю участок по координатам и формирую экспресс-оценку."
)

COORDS_FAILED_TEXT = (
    "Не удалось определить участок по координатам.\n\n"
    "В этой точке нет зарегистрированного участка — проверьте "
    "координаты или отправьте кадастровый номер, например 50:03:0070305:370."
)

LISTING_FAILED_TEXT = (
    "Не удалось определить участок по ссылке на объявление.\n\n"
    "Метка продавца может отсутствовать или не совпадать с границами "
    "участка. Отправьте кадастровый номер, например 50:03:0070305:370."
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

FEEDBACK_SAVED_TEXT = "Спасибо! Ваша оценка сохранена."

FEEDBACK_FAILED_TEXT = "Не удалось сохранить оценку. Попробуйте позже."

_FEEDBACK_PREFIX = "fb"
_FEEDBACK_VOTES = {"up", "down"}


def feedback_keyboard(cadastral_number: str) -> InlineKeyboardMarkup:
    """Клавиатура под PDF: 👍 / 👎 с привязкой к номеру + вход в поддержку."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👍",
                    callback_data=f"{_FEEDBACK_PREFIX}:up:{cadastral_number}",
                ),
                InlineKeyboardButton(
                    text="👎",
                    callback_data=f"{_FEEDBACK_PREFIX}:down:{cadastral_number}",
                ),
            ],
            [support_start_button()],
        ]
    )


def parse_feedback_data(data: str) -> tuple[str, str] | None:
    """callback_data «fb:up:50:21:0030210:123» → (vote, КН); мусор → None."""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != _FEEDBACK_PREFIX:
        return None
    vote = parts[1]
    cadastral_number = parse_cadastral_number(parts[2])
    if vote not in _FEEDBACK_VOTES or cadastral_number is None:
        return None
    return vote, cadastral_number


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


_RESOLUTION_WARNING_LABELS = (
    (
        "area_mismatch",
        (
            "⚠️ Площадь в объявлении не совпадает с данными реестра — "
            "метка продавца могла указывать на соседний участок."
        ),
    ),
    (
        "previously_registered_plot",
        (
            "⚠️ Участок имеет статус «ранее учтённый» — "
            "его границы могут быть устаревшими."
        ),
    ),
    (
        "multiple_landplots_at_coordinates",
        "⚠️ По метке найдено несколько участков — показан первый из них.",
    ),
)


def _render_nearby_lines(nearby: list[dict[str, Any]]) -> list[str]:
    """Строки «• КН · площадь · расстояние» для ближайших участков."""
    lines = []
    for item in nearby:
        cadastral_number = item.get("cadastral_number")
        if not cadastral_number:
            continue
        parts = [f"• {cadastral_number}"]
        area_m2 = item.get("area_m2")
        if isinstance(area_m2, (int, float)) and area_m2 > 0:
            parts.append(f"{area_m2 / 100:g} сот.")
        distance_m = item.get("distance_m")
        if isinstance(distance_m, (int, float)) and distance_m > 0:
            parts.append(f"≈{distance_m:g} м от метки")
        lines.append(" · ".join(parts))
    return lines


def render_listing_failed_text(exc: ListingResolutionFailed) -> str:
    """Текст отказа резолюции; с участками рядом, если движок их нашёл."""
    if not exc.nearby:
        return LISTING_FAILED_TEXT
    lines = [
        (
            "Метка продавца в объявлении приблизительная: точка не попадает "
            "в границы зарегистрированного участка."
        ),
        "",
        "Участки рядом с меткой:",
        *_render_nearby_lines(exc.nearby),
        "",
        (
            "Если один из них — участок из объявления, отправьте его "
            "кадастровый номер сообщением."
        ),
    ]
    return "\n".join(lines)


def render_coords_failed_text(exc: ListingResolutionFailed) -> str:
    """Текст отказа резолюции координат; с участками рядом, если найдены."""
    if not exc.nearby:
        return COORDS_FAILED_TEXT
    lines = [
        "В указанной точке нет зарегистрированного участка.",
        "",
        "Участки рядом с точкой:",
        *_render_nearby_lines(exc.nearby),
        "",
        (
            "Если один из них — нужный участок, отправьте его "
            "кадастровый номер сообщением."
        ),
    ]
    return "\n".join(lines)


def render_resolution_warning(warning: str) -> str:
    """Статический русский текст предупреждения резолюции (без LLM)."""
    for prefix, label in _RESOLUTION_WARNING_LABELS:
        if warning.startswith(prefix):
            return label
    return f"⚠️ {_short(warning)}"


def render_resolution_note(resolution: ListingResolution) -> str:
    """Сообщение о найденном участке + предупреждения резолюции."""
    if resolution.source == "coordinates":
        header = f"По координатам найден участок {resolution.cadastral_number}."
    else:
        header = f"По объявлению найден участок {resolution.cadastral_number}."
    parts = [header]
    if resolution.address:
        parts.append(resolution.address)
    if resolution.warnings:
        parts.append("")
        parts.extend(
            render_resolution_warning(warning)
            for warning in resolution.warnings
        )
    return "\n".join(parts)


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
    client: TerraLogicClient,
    semaphore: asyncio.Semaphore,
    jobs: dict[tuple[int, str], asyncio.Task],
    key: tuple[int, str],
    request_id: str,
    cadastral_number: str | None = None,
    listing_url: str | None = None,
    coordinates: str | None = None,
) -> None:
    """Фоновая задача: существующий use case TerraLogicX → PDF → Telegram.

    Три входа: кадастровый номер, ссылка на объявление либо координаты
    (резолюция в КН выполняется контуром engine на стороне сервера).
    """
    started = monotonic()
    user_id = key[0]
    label = cadastral_number or listing_url or coordinates or "?"
    try:
        async with semaphore:
            if listing_url is not None:
                result = await client.prepare_quickreport_from_listing(listing_url)
            elif coordinates is not None:
                result = await client.prepare_quickreport_at_coords(coordinates)
            elif cadastral_number is not None:
                result = await client.prepare_quickreport(cadastral_number)
            else:  # pragma: no cover - защищено диспетчеризацией handler'а
                raise BotError("report job без входных данных")
        resolved = result.cadastral_number
        if result.resolution is not None:
            await message.answer(render_resolution_note(result.resolution))
        filename = report_filename(resolved)
        await message.answer_document(
            BufferedInputFile(result.pdf_bytes, filename=filename),
            caption=f"Экспресс-оценка участка\n{resolved}",
            reply_markup=feedback_keyboard(resolved),
        )
        LOGGER.info(
            "%s done: user=%s kn=%s case=%s run=%s %.1fs",
            request_id,
            user_id,
            resolved,
            result.case_id,
            result.collection_run_id,
            monotonic() - started,
        )
    except ListingResolutionFailed as exc:
        if coordinates is not None:
            await message.answer(render_coords_failed_text(exc))
        else:
            await message.answer(render_listing_failed_text(exc))
        LOGGER.info(
            "%s listing resolution failed: user=%s input=%s error=%s",
            request_id, user_id, label, exc,
        )
    except ParcelNotFound:
        await message.answer(NOT_FOUND_TEXT.format(cadastral_number=label))
        LOGGER.info("%s not found: user=%s kn=%s", request_id, user_id, label)
    except (SourceUnavailable, TerraLogicUnavailable) as exc:
        await message.answer(UNAVAILABLE_TEXT)
        LOGGER.warning(
            "%s unavailable: user=%s kn=%s error=%s",
            request_id, user_id, label, exc,
        )
    except BotError as exc:
        await message.answer(FAILED_TEXT)
        LOGGER.warning(
            "%s failed: user=%s kn=%s error=%s: %s",
            request_id, user_id, label, type(exc).__name__, exc,
        )
    except Exception:  # последний рубеж, traceback только в лог
        await message.answer(FAILED_TEXT)
        LOGGER.exception(
            "%s unexpected: user=%s kn=%s", request_id, user_id, label
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
        text = message.text or ""
        cadastral_number = parse_cadastral_number(text)
        listing_url = parse_listing_url(text)
        coordinates = parse_coordinates(text)
        if cadastral_number is None and listing_url is not None:
            # «Номер + ссылка» в одном сообщении — явный номер важнее.
            cadastral_number = extract_cadastral_number(text)
        if cadastral_number is None and listing_url is None and coordinates is None:
            await message.answer(INVALID_TEXT)
            return
        user_id = message.from_user.id if message.from_user else 0
        request_id = uuid.uuid4().hex[:8]
        if cadastral_number is not None:
            key = (user_id, cadastral_number)
            if key in jobs:
                await message.answer(DUPLICATE_TEXT)
                return
            LOGGER.info(
                "%s accepted: user=%s kn=%s", request_id, user_id, cadastral_number
            )
            await message.answer(
                f"Принял участок {cadastral_number}.\n\nФормирую экспресс-оценку."
            )
            task = asyncio.create_task(
                _report_job(
                    message=message,
                    client=client,
                    semaphore=semaphore,
                    jobs=jobs,
                    key=key,
                    request_id=request_id,
                    cadastral_number=cadastral_number,
                )
            )
        elif listing_url is not None:
            key = (user_id, listing_url)
            if key in jobs:
                await message.answer(DUPLICATE_TEXT)
                return
            LOGGER.info(
                "%s accepted listing: user=%s url=%s",
                request_id, user_id, listing_url,
            )
            await message.answer(LISTING_ACCEPTED_TEXT)
            task = asyncio.create_task(
                _report_job(
                    message=message,
                    client=client,
                    semaphore=semaphore,
                    jobs=jobs,
                    key=key,
                    request_id=request_id,
                    listing_url=listing_url,
                )
            )
        else:
            assert coordinates is not None  # гарантировано веткой INVALID_TEXT
            key = (user_id, coordinates)
            if key in jobs:
                await message.answer(DUPLICATE_TEXT)
                return
            LOGGER.info(
                "%s accepted coords: user=%s coords=%s",
                request_id, user_id, coordinates,
            )
            await message.answer(COORDS_ACCEPTED_TEXT)
            task = asyncio.create_task(
                _report_job(
                    message=message,
                    client=client,
                    semaphore=semaphore,
                    jobs=jobs,
                    key=key,
                    request_id=request_id,
                    coordinates=coordinates,
                )
            )
        jobs[key] = task

    @router.callback_query(F.data.startswith(f"{_FEEDBACK_PREFIX}:"))
    async def feedback(query: CallbackQuery) -> None:
        parsed = parse_feedback_data(query.data or "")
        if parsed is None:
            await query.answer()
            return
        vote, cadastral_number = parsed
        user_id = query.from_user.id if query.from_user else None
        try:
            await client.submit_feedback(
                cadastral_number,
                vote,
                user_id=user_id,
            )
        except BotError as exc:
            LOGGER.warning(
                "feedback failed: user=%s kn=%s vote=%s error=%s: %s",
                user_id, cadastral_number, vote, type(exc).__name__, exc,
            )
            await query.answer(FEEDBACK_FAILED_TEXT)
            return
        except Exception:  # последний рубеж, traceback только в лог
            LOGGER.exception(
                "feedback unexpected: user=%s kn=%s", user_id, cadastral_number
            )
            await query.answer(FEEDBACK_FAILED_TEXT)
            return
        LOGGER.info(
            "feedback saved: user=%s kn=%s vote=%s", user_id, cadastral_number, vote
        )
        await query.answer(FEEDBACK_SAVED_TEXT)
        message = query.message
        if message is not None:
            try:
                await message.edit_reply_markup(reply_markup=None)
            except TelegramAPIError:  # устаревшее сообщение — оценка уже сохранена
                LOGGER.debug(
                    "feedback keyboard not removed: kn=%s", cadastral_number
                )

    return router
