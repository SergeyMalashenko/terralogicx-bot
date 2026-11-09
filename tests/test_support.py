"""Тесты сценария поддержки: SQLite-стор и router (без Telegram API)."""

from __future__ import annotations

import time
from types import SimpleNamespace

from terralogic_bot.config import Settings
from terralogic_bot.support import (
    SUPPORT_DISABLED_TEXT,
    SUPPORT_NOT_ACTIVE_TEXT,
    SUPPORT_OPEN_TEXT,
    SUPPORT_REPLY_PREFIX,
    SUPPORT_SENT_TEXT,
    SupportStore,
    _InSupportSession,
    build_support_router,
)

GROUP_ID = -1001234567890
USER_ID = 42


def _settings(group_id: int | None = GROUP_ID, ttl_s: float = 3600.0) -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        terralogic_mcp_url="http://127.0.0.1:8004/mcp",
        request_timeout_s=5.0,
        max_concurrent_reports=1,
        log_level="DEBUG",
        support_group_id=group_id,
        support_session_ttl_s=ttl_s,
        support_db_path=":memory:",
    )


class FakeUser:
    def __init__(
        self,
        user_id: int,
        full_name: str = "Иван Иванов",
        username: str | None = "ivan",
        is_bot: bool = False,
    ) -> None:
        self.id = user_id
        self.full_name = full_name
        self.username = username
        self.is_bot = is_bot


class FakeMessage:
    """Минимальный контракт aiogram Message для support-router."""

    def __init__(
        self,
        text: str | None,
        *,
        user_id: int = USER_ID,
        chat_id: int = USER_ID,
        chat_type: str = "private",
        message_id: int = 1,
        reply_to_message_id: int | None = None,
        is_bot: bool = False,
    ) -> None:
        self.text = text
        self.from_user = FakeUser(user_id, is_bot=is_bot)
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.message_id = message_id
        self.reply_to_message = (
            SimpleNamespace(message_id=reply_to_message_id)
            if reply_to_message_id is not None
            else None
        )
        self.answers: list[str] = []
        self.answer_kwargs: list[dict] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append(text)
        self.answer_kwargs.append(dict(kwargs))


class FakeCallbackQuery:
    def __init__(self, data: str, user_id: int = USER_ID) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(None, user_id=user_id)
        self.answered: list[str | None] = []

    async def answer(self, text: str | None = None, **kwargs: object) -> None:
        self.answered.append(text)


class FakeBot:
    """Записывает send_message/copy_message; выдаёт нарастающие id."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.copied: list[tuple[int, int, int]] = []
        self._next_id = 1000

    def _message_id(self) -> SimpleNamespace:
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_message(self, chat_id: int, text: str, **kwargs: object):
        self.sent.append((chat_id, text))
        return self._message_id()

    async def copy_message(
        self, chat_id: int, from_chat_id: int, message_id: int, **kwargs: object
    ):
        self.copied.append((chat_id, from_chat_id, message_id))
        return self._message_id()


def _handler(router, name: str):
    for observer in router.message.handlers:
        if observer.callback.__name__ == name:
            return observer.callback
    raise AssertionError(f"handler {name} не зарегистрирован")


def _callback_handler(router, name: str):
    for observer in router.callback_query.handlers:
        if observer.callback.__name__ == name:
            return observer.callback
    raise AssertionError(f"callback handler {name} не зарегистрирован")


# --- SupportStore ---------------------------------------------------------


def test_store_session_lifecycle(tmp_path):
    store = SupportStore(tmp_path / "support.sqlite3")
    assert store.session_age(USER_ID) is None

    store.open_session(USER_ID)
    age = store.session_age(USER_ID)
    assert age is not None and age < 5

    store.close_session(USER_ID)
    assert store.session_age(USER_ID) is None
    assert store.close_session(USER_ID) is False
    store.close()


def test_store_expire_sessions(tmp_path):
    store = SupportStore(tmp_path / "support.sqlite3")
    store.open_session(USER_ID)
    store.open_session(USER_ID + 1)
    store._conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE user_id = ?",
        (time.time() - 7200, USER_ID),
    )
    expired = store.expire_sessions(3600.0)
    assert expired == [USER_ID]
    assert store.session_age(USER_ID) is None
    assert store.session_age(USER_ID + 1) is not None
    store.close()


def test_store_links_roundtrip_and_persistence(tmp_path):
    path = tmp_path / "support.sqlite3"
    store = SupportStore(path)
    store.add_link(555, USER_ID)
    store.add_link(556, USER_ID + 1)
    store.close()

    reopened = SupportStore(path)
    assert reopened.resolve_user(555) == USER_ID
    assert reopened.resolve_user(556) == USER_ID + 1
    assert reopened.resolve_user(999) is None
    reopened.close()


# --- фильтр сессии --------------------------------------------------------


def test_session_filter_matches_only_active_fresh_sessions(tmp_path):
    store = SupportStore(tmp_path / "support.sqlite3")
    filter_ = _InSupportSession(store, ttl_s=3600.0)
    message = FakeMessage("привет")

    assert filter_(message) is False
    store.open_session(USER_ID)
    assert filter_(message) is True

    store._conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE user_id = ?",
        (time.time() - 7200, USER_ID),
    )
    assert filter_(message) is False
    store.close()


# --- router ---------------------------------------------------------------


async def test_support_cmd_opens_session(tmp_path):
    store = SupportStore(tmp_path / "support.sqlite3")
    router = build_support_router(_settings(), store)
    message = FakeMessage("/support")
    await _handler(router, "support_cmd")(message)

    assert message.answers == [SUPPORT_OPEN_TEXT]
    assert store.session_age(USER_ID) is not None
    markup = message.answer_kwargs[0].get("reply_markup")
    assert markup is not None
    (row,) = markup.inline_keyboard
    assert row[0].callback_data == "support:stop"
    store.close()


async def test_support_cmd_disabled_without_group(tmp_path):
    store = SupportStore(tmp_path / "support.sqlite3")
    router = build_support_router(_settings(group_id=None), store)
    message = FakeMessage("/support")
    await _handler(router, "support_cmd")(message)

    assert message.answers == [SUPPORT_DISABLED_TEXT]
    assert store.session_age(USER_ID) is None
    store.close()


async def test_support_callback_start_and_stop(tmp_path):
    store = SupportStore(tmp_path / "support.sqlite3")
    router = build_support_router(_settings(), store)
    handler = _callback_handler(router, "support_callback")

    start = FakeCallbackQuery("support:start")
    await handler(start)
    assert store.session_age(USER_ID) is not None
    assert start.message.answers == [SUPPORT_OPEN_TEXT]

    stop = FakeCallbackQuery("support:stop")
    await handler(stop)
    assert store.session_age(USER_ID) is None

    again = FakeCallbackQuery("support:stop")
    await handler(again)
    assert again.answered == [SUPPORT_NOT_ACTIVE_TEXT]
    store.close()


async def test_relay_to_group_copies_message_and_links_ids(tmp_path):
    store = SupportStore(tmp_path / "support.sqlite3")
    store.open_session(USER_ID)
    router = build_support_router(_settings(), store)
    bot = FakeBot()
    message = FakeMessage("не приходит отчёт", chat_id=USER_ID, message_id=77)
    await _handler(router, "relay_to_group")(message, bot)

    assert message.answers == [SUPPORT_SENT_TEXT]
    (header_chat, header_text), = bot.sent
    assert header_chat == GROUP_ID
    assert "Иван Иванов" in header_text and f"id {USER_ID}" in header_text
    (copy_chat, from_chat, copy_id), = bot.copied
    assert (copy_chat, from_chat, copy_id) == (GROUP_ID, USER_ID, 77)

    # оба сообщения в группе маршрутизируют ответ обратно пользователю
    header_id = 1001
    copy_msg_id = 1002
    assert store.resolve_user(header_id) == USER_ID
    assert store.resolve_user(copy_msg_id) == USER_ID
    store.close()


async def test_relay_to_user_routes_reply_back(tmp_path):
    store = SupportStore(tmp_path / "support.sqlite3")
    store.open_session(USER_ID)
    store.add_link(2001, USER_ID)
    router = build_support_router(_settings(), store)
    bot = FakeBot()
    operator_reply = FakeMessage(
        "проверьте номер ещё раз",
        user_id=900,
        chat_id=GROUP_ID,
        chat_type="supergroup",
        message_id=2002,
        reply_to_message_id=2001,
    )
    await _handler(router, "relay_to_user")(operator_reply, bot)

    assert bot.sent == [(USER_ID, SUPPORT_REPLY_PREFIX)]
    assert bot.copied == [(USER_ID, GROUP_ID, 2002)]
    store.close()


async def test_relay_to_user_ignores_foreign_replies_and_bots(tmp_path):
    store = SupportStore(tmp_path / "support.sqlite3")
    router = build_support_router(_settings(), store)
    bot = FakeBot()
    handler = _handler(router, "relay_to_user")

    # реплай на чужое сообщение (не из links)
    foreign = FakeMessage(
        "комментарий",
        user_id=900,
        chat_id=GROUP_ID,
        chat_type="supergroup",
        reply_to_message_id=424242,
    )
    await handler(foreign, bot)
    # сообщение другого бота
    from_bot = FakeMessage(
        "echo",
        user_id=901,
        chat_id=GROUP_ID,
        chat_type="supergroup",
        reply_to_message_id=2001,
        is_bot=True,
    )
    store.add_link(2001, USER_ID)
    await handler(from_bot, bot)
    # сообщение без реплая
    plain = FakeMessage("просто текст", user_id=900, chat_id=GROUP_ID)
    await handler(plain, bot)

    assert bot.sent == []
    assert bot.copied == []
    store.close()
