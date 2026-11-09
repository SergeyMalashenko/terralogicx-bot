"""Диалог с поддержкой через служебную группу Telegram.

Пользователь входит в режим диалога (/support или кнопка «Написать в
поддержку»); его сообщения копируются в группу поддержки со служебной
шапкой. Ответы операторов (reply на сообщения бота в группе) копируются
обратно пользователю. Сессии и маршрутизация ответов — в SQLite и
переживают перезапуск бота. Пока SUPPORT_GROUP_ID не задан, сценарий
отвечает заглушкой, остальной бот работает как раньше.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .config import Settings

LOGGER = logging.getLogger("terralogic_bot.support")

_SUPPORT_CALLBACK_PREFIX = "support"

SUPPORT_DISABLED_TEXT = (
    "Служба поддержки пока не подключена.\n\n"
    "Если с отчётом что-то не так, оставьте оценку 👎 под PDF — "
    "она сохраняется для участка."
)

SUPPORT_OPEN_TEXT = (
    "Вы в диалоге со службой поддержки.\n\n"
    "Опишите вопрос или проблему — сообщения будут переданы операторам. "
    "Можно отправлять текст, фото и документы.\n\n"
    "Чтобы выйти из диалога, нажмите «Завершить диалог» или отправьте "
    "/support ещё раз. Диалог завершается автоматически через час "
    "без активности."
)

SUPPORT_CLOSE_TEXT = (
    "Диалог с поддержкой завершён.\n\n"
    "Чтобы получить отчёт, отправьте кадастровый номер участка."
)

SUPPORT_NOT_ACTIVE_TEXT = "Диалог с поддержкой не активен."

SUPPORT_EXPIRED_TEXT = (
    "Диалог с поддержкой завершён из-за долгого отсутствия активности.\n\n"
    "Чтобы написать снова, отправьте /support."
)

SUPPORT_SENT_TEXT = "Передано в поддержку ✅"

SUPPORT_FAILED_TEXT = (
    "Не удалось передать сообщение в поддержку.\n\nПопробуйте позже."
)

SUPPORT_REPLY_PREFIX = "Ответ поддержки:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    user_id INTEGER PRIMARY KEY,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    group_message_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""


class SupportStore:
    """SQLite-хранилище сессий диалога и маршрутизации ответов группы."""

    def __init__(self, path: str | Path) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # aiogram вызывает sync-фильтры через asyncio.to_thread — соединение
        # обязано переживать чужой поток; операции короткие, под lock.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def open_session(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (user_id, updated_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) "
                "DO UPDATE SET updated_at = excluded.updated_at",
                (user_id, time.time()),
            )
            self._conn.commit()

    def touch_session(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE user_id = ?",
                (time.time(), user_id),
            )
            self._conn.commit()

    def close_session(self, user_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE user_id = ?", (user_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def session_age(self, user_id: int) -> float | None:
        """Секунд с последней активности; None, если сессии нет."""
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_at FROM sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return time.time() - float(row[0])

    def expire_sessions(self, ttl_s: float) -> list[int]:
        """Удалить сессии без активности дольше ttl_s; вернуть их user_id."""
        threshold = time.time() - ttl_s
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id FROM sessions WHERE updated_at < ?", (threshold,)
            ).fetchall()
            self._conn.execute(
                "DELETE FROM sessions WHERE updated_at < ?", (threshold,)
            )
            self._conn.commit()
        return [int(row[0]) for row in rows]

    def add_link(self, group_message_id: int, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO links"
                " (group_message_id, user_id, created_at) VALUES (?, ?, ?)",
                (group_message_id, user_id, time.time()),
            )
            self._conn.commit()

    def resolve_user(self, group_message_id: int) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id FROM links WHERE group_message_id = ?",
                (group_message_id,),
            ).fetchone()
        return int(row[0]) if row is not None else None


def support_start_button() -> InlineKeyboardButton:
    """Кнопка входа в диалог (добавляется под отчёт рядом с 👍/👎)."""
    return InlineKeyboardButton(
        text="✉️ Написать в поддержку",
        callback_data=f"{_SUPPORT_CALLBACK_PREFIX}:start",
    )


def support_stop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Завершить диалог",
                    callback_data=f"{_SUPPORT_CALLBACK_PREFIX}:stop",
                )
            ]
        ]
    )


class _InSupportSession:
    """Фильтр: сообщение от пользователя с активной сессией поддержки."""

    def __init__(self, store: SupportStore, ttl_s: float) -> None:
        self._store = store
        self._ttl_s = ttl_s

    def __call__(self, message: Message) -> bool:
        user = message.from_user
        if user is None:
            return False
        age = self._store.session_age(user.id)
        return age is not None and age < self._ttl_s


def _user_header(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "👤 неизвестный отправитель"
    parts = [f"👤 {user.full_name}"]
    if user.username:
        parts.append(f"@{user.username}")
    parts.append(f"· id {user.id}")
    return " ".join(parts)


def build_support_router(settings: Settings, store: SupportStore) -> Router:
    """Router поддержки: регистрируется ДО основного, перехватывает
    сообщения пользователей в активной сессии и весь трафик группы."""
    router = Router(name="terralogicx-support")
    group_id = settings.support_group_id

    async def _open(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        if group_id is None:
            await message.answer(SUPPORT_DISABLED_TEXT)
            return
        store.open_session(user.id)
        await message.answer(SUPPORT_OPEN_TEXT, reply_markup=support_stop_keyboard())
        LOGGER.info("support open: user=%s", user.id)

    @router.message(Command("support"))
    async def support_cmd(message: Message) -> None:
        await _open(message)

    @router.callback_query(F.data.startswith(f"{_SUPPORT_CALLBACK_PREFIX}:"))
    async def support_callback(query: CallbackQuery) -> None:
        action = (query.data or "").split(":", 1)[1]
        user = query.from_user
        if action == "start":
            if group_id is None:
                await query.answer(SUPPORT_DISABLED_TEXT)
                return
            store.open_session(user.id)
            await query.answer()
            if query.message is not None:
                await query.message.answer(
                    SUPPORT_OPEN_TEXT, reply_markup=support_stop_keyboard()
                )
            LOGGER.info("support open (button): user=%s", user.id)
            return
        if action == "stop":
            closed = store.close_session(user.id)
            text = SUPPORT_CLOSE_TEXT if closed else SUPPORT_NOT_ACTIVE_TEXT
            await query.answer(text)
            LOGGER.info("support close: user=%s closed=%s", user.id, closed)
            return
        await query.answer()

    in_session = _InSupportSession(store, settings.support_session_ttl_s)

    @router.message(F.chat.type == "private", in_session)
    async def relay_to_group(message: Message, bot: Bot) -> None:
        user = message.from_user
        if group_id is None:  # конфигурацию убрали при активной сессии
            store.close_session(user.id)
            await message.answer(SUPPORT_DISABLED_TEXT)
            return
        try:
            header = await bot.send_message(group_id, _user_header(message))
            copy = await bot.copy_message(
                group_id, message.chat.id, message.message_id
            )
        except TelegramAPIError:
            LOGGER.warning(
                "support relay failed: user=%s", user.id, exc_info=True
            )
            await message.answer(SUPPORT_FAILED_TEXT)
            return
        store.add_link(header.message_id, user.id)
        store.add_link(copy.message_id, user.id)
        store.touch_session(user.id)
        await message.answer(SUPPORT_SENT_TEXT, reply_markup=support_stop_keyboard())
        LOGGER.info(
            "support → group: user=%s group_msg=%s", user.id, copy.message_id
        )

    @router.message(F.chat.id == group_id)
    async def relay_to_user(message: Message, bot: Bot) -> None:
        sender = message.from_user
        if sender is not None and sender.is_bot:
            return
        reply = message.reply_to_message
        if reply is None:
            return
        user_id = store.resolve_user(reply.message_id)
        if user_id is None:
            return
        try:
            await bot.send_message(user_id, SUPPORT_REPLY_PREFIX)
            await bot.copy_message(user_id, message.chat.id, message.message_id)
        except TelegramAPIError:
            LOGGER.warning(
                "support reply to user %s failed", user_id, exc_info=True
            )
            return
        if store.session_age(user_id) is not None:
            store.touch_session(user_id)
        LOGGER.info("support → user: user=%s", user_id)

    @router.my_chat_member()
    async def membership(event: ChatMemberUpdated) -> None:
        # id группы для SUPPORT_GROUP_ID берётся из этого лога
        LOGGER.info(
            "chat membership: chat_id=%s title=%r status=%s",
            event.chat.id,
            event.chat.title,
            event.new_chat_member.status,
        )

    return router


async def support_sweeper(
    bot: Bot,
    store: SupportStore,
    ttl_s: float,
    interval_s: float = 60.0,
) -> None:
    """Фоновая задача: завершает просроченные сессии с уведомлением."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            expired = store.expire_sessions(ttl_s)
        except sqlite3.Error:
            LOGGER.warning("support sweeper: ошибка SQLite", exc_info=True)
            continue
        for user_id in expired:
            try:
                await bot.send_message(user_id, SUPPORT_EXPIRED_TEXT)
            except TelegramAPIError:
                LOGGER.debug("support expired notify failed: user=%s", user_id)
            LOGGER.info("support expired: user=%s", user_id)
