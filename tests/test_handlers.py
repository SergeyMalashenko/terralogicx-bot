"""Тесты Telegram handlers с fake client и fake message (без Telegram API)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

from terralogic_bot.config import Settings
from terralogic_bot.errors import (
    CaseNotCollected,
    ListingResolutionFailed,
    ParcelNotFound,
    ReportGenerationFailed,
    TerraLogicUnavailable,
)
from terralogic_bot.handlers import (
    DUPLICATE_TEXT,
    FAILED_TEXT,
    FEEDBACK_FAILED_TEXT,
    FEEDBACK_SAVED_TEXT,
    HELP_TEXT,
    INVALID_TEXT,
    LISTING_ACCEPTED_TEXT,
    LISTING_FAILED_TEXT,
    NOT_COLLECTED_TEXT,
    NOT_FOUND_TEXT,
    START_TEXT,
    STATUS_USAGE_TEXT,
    UNAVAILABLE_TEXT,
    _report_job,
    build_router,
    render_progress,
    report_filename,
)
from terralogic_bot.models import (
    CollectionStatus,
    DocumentStatus,
    ListingResolution,
    QuickReportResult,
    SourceStatus,
)
from terralogic_bot.quota import QuotaStore

KN = "50:21:0030210:123"
LISTING_URL = (
    "https://www.avito.ru/domodedovo/zemelnye_uchastki/uchastok_10_sot._izhs_8110049401"
)


class _FakeUser:
    def __init__(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class FakeSentMessage:
    """Отправленное сообщение: история правок и факт удаления."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text: str, **kwargs: object) -> None:
        self.edits.append(text)
        self.text = text

    async def delete(self) -> None:
        self.deleted = True


class FakeMessage:
    """Минимальный контракт aiogram Message, используемый handlers."""

    def __init__(self, text: str | None, user_id: int = 42, **user_kw) -> None:
        self.text = text
        self.from_user = _FakeUser(user_id, **user_kw)
        self.answers: list[str] = []
        # Статус прогресса («⏳ …») хранится отдельно, чтобы не смешивать
        # служебные правки с содержательными ответами бота.
        self.status_messages: list[FakeSentMessage] = []
        self.documents: list[tuple[object, str | None]] = []
        self.document_kwargs: list[dict] = []
        self.markups: list[object] = []
        self.markup_removed = False

    async def answer(self, text: str, **kwargs: object) -> FakeSentMessage:
        sent = FakeSentMessage(text)
        self.markups.append(kwargs.get("reply_markup"))
        if text.startswith("⏳"):
            self.status_messages.append(sent)
        else:
            self.answers.append(text)
        return sent

    async def answer_document(
        self, document: object, caption: str | None = None, **kwargs: object
    ) -> None:
        self.documents.append((document, caption))
        self.document_kwargs.append(dict(kwargs))

    async def edit_reply_markup(self, reply_markup: object = None) -> None:
        if reply_markup is None:
            self.markup_removed = True


class FlakyAckMessage(FakeMessage):
    """Первый answer (ack) падает сетевой ошибкой — как при сбое до Telegram."""

    def __init__(self, text: str | None, user_id: int = 42) -> None:
        super().__init__(text, user_id)
        self._ack_failed = False

    async def answer(self, text: str, **kwargs: object) -> None:
        if not self._ack_failed:
            self._ack_failed = True
            raise TelegramNetworkError(
                method=SendMessage(chat_id=self.from_user.id, text=text),
                message="ClientConnectorError: Connection reset by peer",
            )
        await super().answer(text, **kwargs)


class FakeCallbackQuery:
    """Минимальный контракт aiogram CallbackQuery для оценки отчёта."""

    def __init__(self, data: str, user_id: int = 42) -> None:
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = FakeMessage(None, user_id)
        self.answered: list[str | None] = []

    async def answer(self, text: str | None = None, **kwargs: object) -> None:
        self.answered.append(text)


PDF_BYTES = b"%PDF-1.4 canonical"


class FakeClient:
    """Контролируемый backend: задержка, счётчики вызовов и параллелизма."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.listing_calls: list[str] = []
        self.coords_calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.error: Exception | None = None
        self.listing_error: Exception | None = None
        self.coords_error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.status_error: Exception | None = None
        self.status_payload: CollectionStatus | None = None
        self.feedback_calls: list[tuple[str, str, int | None]] = []
        self.feedback_error: Exception | None = None

    async def prepare_quickreport(
        self, cadastral_number: str, *, progress_callback=None
    ) -> QuickReportResult:
        self.calls.append(cadastral_number)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0.01)
            if self.error is not None:
                raise self.error
            if progress_callback is not None:
                for percent in (5.0, 40.0, 80.0, 100.0):
                    progress_callback(percent)
            return QuickReportResult(
                cadastral_number=cadastral_number,
                case_id="case-50-21-0030210-123",
                collection_run_id="run-1",
                pdf_bytes=PDF_BYTES,
            )
        finally:
            self.active -= 1

    async def prepare_quickreport_from_listing(
        self, url: str, *, progress_callback=None
    ) -> QuickReportResult:
        self.listing_calls.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0.01)
            if self.listing_error is not None:
                raise self.listing_error
            if progress_callback is not None:
                for percent in (3.0, 15.0, 60.0, 100.0):
                    progress_callback(percent)
            return QuickReportResult(
                cadastral_number="50:28:0040126:62",
                case_id="case-50-28-0040126-62",
                collection_run_id="run-1",
                pdf_bytes=PDF_BYTES,
                resolution=ListingResolution(
                    url=url,
                    cadastral_number="50:28:0040126:62",
                    title="Участок 10 сот. (ИЖС)",
                    address="МО, г. Домодедово, мкр-н Барыбино",
                    warnings=[
                        "area_mismatch: ...",
                        "previously_registered_plot: ...",
                    ],
                ),
            )
        finally:
            self.active -= 1

    async def prepare_quickreport_at_coords(
        self, coordinates: str, *, progress_callback=None
    ) -> QuickReportResult:
        self.coords_calls.append(coordinates)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0.01)
            if self.coords_error is not None:
                raise self.coords_error
            if progress_callback is not None:
                for percent in (3.0, 15.0, 60.0, 100.0):
                    progress_callback(percent)
            return QuickReportResult(
                cadastral_number="50:23:0000000:120934",
                case_id="case-50-23-0000000-120934",
                collection_run_id="run-1",
                pdf_bytes=PDF_BYTES,
                resolution=ListingResolution(
                    url=coordinates,
                    cadastral_number="50:23:0000000:120934",
                    warnings=[],
                    source="coordinates",
                ),
            )
        finally:
            self.active -= 1

    async def get_collection_status(self, cadastral_number: str) -> CollectionStatus:
        if self.status_error is not None:
            raise self.status_error
        return self.status_payload or CollectionStatus(
            case_id="case-50-21-0030210-123",
            collection_run_id="run-1",
            collection_status="partial",
            reused_collection=True,
            duration_s=187.0,
            sources=[
                SourceStatus(source="nspd", status="ok"),
                SourceStatus(source="osm", status="failed", detail="osm timeout"),
            ],
            documents=[
                DocumentStatus(
                    doc_type="pzz",
                    number="1749",
                    version_date="2026-05-15",
                    status="extracted",
                ),
                DocumentStatus(
                    doc_type="general_plan",
                    status="not_found",
                    reason="агент: документ не найден",
                ),
            ],
        )

    async def submit_feedback(
        self,
        cadastral_number: str,
        vote: str,
        *,
        user_id: int | None = None,
    ) -> None:
        if self.feedback_error is not None:
            raise self.feedback_error
        self.feedback_calls.append((cadastral_number, vote, user_id))


def _settings(tmp_path: Path, max_concurrent: int = 2) -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        terralogic_mcp_url="http://127.0.0.1:8004/mcp",
        request_timeout_s=5.0,
        max_concurrent_reports=max_concurrent,
        log_level="DEBUG",
        report_limit=3,  # в бою 0; здесь проверяем механику квоты
    )


def _handler(router, name: str):
    for observer in router.message.handlers:
        if observer.callback.__name__ == name:
            return observer.callback
    raise AssertionError(f"handler {name} не зарегистрирован")


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("условие не наступило")


async def test_start(tmp_path):
    router = build_router(FakeClient(), _settings(tmp_path))
    message = FakeMessage("/start")
    await _handler(router, "start")(message)
    assert message.answers == [START_TEXT]
    assert message.documents == []  # без DEMO_REPORT_PATH вложения нет


async def test_start_attaches_demo_report_when_configured(tmp_path):
    demo = tmp_path / "demo.pdf"
    demo.write_bytes(b"%PDF-1.4 demo")
    settings = replace(_settings(tmp_path), demo_report_path=str(demo))
    router = build_router(FakeClient(), settings)
    message = FakeMessage("/start")
    await _handler(router, "start")(message)
    assert message.answers == [START_TEXT]
    assert len(message.documents) == 1
    _document, caption = message.documents[0]
    assert caption is not None and "50:33:0010516:820" in caption


async def test_start_missing_demo_file_sends_text_only(tmp_path):
    settings = replace(
        _settings(tmp_path), demo_report_path=str(tmp_path / "missing.pdf")
    )
    router = build_router(FakeClient(), settings)
    message = FakeMessage("/start")
    await _handler(router, "start")(message)
    assert message.answers == [START_TEXT]
    assert message.documents == []


async def test_user_registered_in_quota_store(tmp_path):
    quota = QuotaStore(tmp_path / "quota.sqlite3")
    router = build_router(FakeClient(), _settings(tmp_path), quota)
    message = FakeMessage(
        "/start",
        user_id=798610197,
        username="sergey_malashenko",
        first_name="Sergey",
        last_name="Malashenko",
    )
    await _handler(router, "start")(message)
    users = quota.list_users()
    assert len(users) == 1
    assert users[0]["user_id"] == 798610197
    assert users[0]["username"] == "sergey_malashenko"
    assert users[0]["first_name"] == "Sergey"
    quota.close()


async def test_help(tmp_path):
    router = build_router(FakeClient(), _settings(tmp_path))
    message = FakeMessage("/help")
    await _handler(router, "help_")(message)
    assert message.answers == [HELP_TEXT]


async def test_invalid_cadastral_never_calls_backend(tmp_path):
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage("привет")
    await _handler(router, "cadastral")(message)
    assert message.answers == [INVALID_TEXT]
    assert client.calls == []


async def test_successful_report_delivery(tmp_path):
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)

    assert client.calls == [KN]
    assert message.answers[0].startswith("Принял участок 50:21:0030210:123")
    document, caption = message.documents[0]
    assert document.filename == "TerraLogicX_50-21-0030210-123.pdf"
    assert document.data == PDF_BYTES
    assert caption == f"Экспресс-оценка участка\n{KN}"


async def test_parcel_not_found_message(tmp_path):
    client = FakeClient()
    client.error = ParcelNotFound("нет участка")
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.answers) == 2)
    assert message.answers[-1] == NOT_FOUND_TEXT.format(cadastral_number=KN)


async def test_backend_unavailable_message(tmp_path):
    client = FakeClient()
    client.error = TerraLogicUnavailable("backend down")
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.answers) == 2)
    assert message.answers[-1] == UNAVAILABLE_TEXT


async def test_generation_failure_message(tmp_path):
    client = FakeClient()
    client.error = ReportGenerationFailed("нет pdf")
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.answers) == 2)
    assert message.answers[-1] == FAILED_TEXT


async def test_duplicate_active_request_rejected(tmp_path):
    client = FakeClient()
    client.gate = asyncio.Event()
    router = build_router(client, _settings(tmp_path))
    handler = _handler(router, "cadastral")

    first = FakeMessage(KN)
    await handler(first)
    await _wait_for(lambda: client.active == 1)

    second = FakeMessage(KN)
    await handler(second)  # тот же пользователь и КН, задача ещё активна
    assert second.answers == [DUPLICATE_TEXT]

    client.gate.set()
    await _wait_for(lambda: len(first.documents) == 1)
    assert client.calls == [KN]  # backend вызван один раз


async def test_concurrency_limited_by_semaphore(tmp_path):
    client = FakeClient()
    client.gate = asyncio.Event()
    router = build_router(client, _settings(tmp_path, max_concurrent=1))
    handler = _handler(router, "cadastral")

    messages = [FakeMessage(f"50:21:0030210:12{i}") for i in range(3)]
    for message in messages:
        await handler(message)
    await _wait_for(lambda: client.active == 1)
    await asyncio.sleep(0.05)
    assert client.max_active == 1  # семафор удерживает один активный отчёт
    client.gate.set()
    await _wait_for(lambda: all(len(m.documents) == 1 for m in messages))
    assert client.max_active == 1
    assert len(client.calls) == 3


def test_report_filename_safe():
    assert report_filename("50:21:0030210:123") == "TerraLogicX_50-21-0030210-123.pdf"


class _FakeCommand:
    """Минимальный CommandObject: аргументы команды."""

    def __init__(self, args: str | None) -> None:
        self.args = args


async def test_status_usage_without_argument(tmp_path):
    router = build_router(FakeClient(), _settings(tmp_path))
    message = FakeMessage("/status")
    await _handler(router, "status")(message, _FakeCommand(None))
    assert message.answers == [STATUS_USAGE_TEXT]


async def test_status_usage_on_invalid_argument(tmp_path):
    router = build_router(FakeClient(), _settings(tmp_path))
    message = FakeMessage("/status привет")
    await _handler(router, "status")(message, _FakeCommand("привет"))
    assert message.answers == [STATUS_USAGE_TEXT]


async def test_status_ok_renders_summary(tmp_path):
    router = build_router(FakeClient(), _settings(tmp_path))
    message = FakeMessage(f"/status {KN}")
    await _handler(router, "status")(message, _FakeCommand(KN))

    assert len(message.answers) == 1
    text = message.answers[0]
    assert f"Сводка сбора: {KN}" in text
    assert "собрано частично" in text
    assert "данные из кэша" in text
    assert "✅ НСПД" in text
    assert "❌ OpenStreetMap — osm timeout" in text
    assert "✅ ПЗЗ № 1749 от 2026-05-15 — данные извлечены" in text
    assert "❌ Генплан — не найден (агент: документ не найден)" in text


async def test_status_not_collected(tmp_path):
    client = FakeClient()
    client.status_error = CaseNotCollected("no run")
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(f"/status {KN}")
    await _handler(router, "status")(message, _FakeCommand(KN))
    assert message.answers == [NOT_COLLECTED_TEXT.format(cadastral_number=KN)]


async def test_status_backend_unavailable(tmp_path):
    client = FakeClient()
    client.status_error = TerraLogicUnavailable("refused")
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(f"/status {KN}")
    await _handler(router, "status")(message, _FakeCommand(KN))
    assert message.answers == [UNAVAILABLE_TEXT]


# ---------------------------------------------------------------------------
# Оценка отчёта 👍/👎
# ---------------------------------------------------------------------------


def _callback_handler(router, name: str):
    for observer in router.callback_query.handlers:
        if observer.callback.__name__ == name:
            return observer.callback
    raise AssertionError(f"callback handler {name} не зарегистрирован")


async def test_report_document_carries_feedback_keyboard(tmp_path):
    router = build_router(FakeClient(), _settings(tmp_path))
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)

    keyboard = message.document_kwargs[0].get("reply_markup")
    assert keyboard is not None
    vote_row, support_row = keyboard.inline_keyboard
    assert [button.text for button in vote_row] == ["👍", "👎"]
    assert [button.callback_data for button in vote_row] == [
        f"fb:up:{KN}",
        f"fb:down:{KN}",
    ]
    assert [button.text for button in support_row] == ["✉️ Написать в поддержку"]
    assert [button.callback_data for button in support_row] == ["support:start"]


async def test_feedback_vote_saved(tmp_path):
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    query = FakeCallbackQuery(f"fb:up:{KN}", user_id=777)
    await _callback_handler(router, "feedback")(query)

    assert client.feedback_calls == [(KN, "up", 777)]
    assert query.answered == [FEEDBACK_SAVED_TEXT]
    assert query.message.markup_removed is True


async def test_feedback_backend_failure_keeps_keyboard(tmp_path):
    client = FakeClient()
    client.feedback_error = TerraLogicUnavailable("backend down")
    router = build_router(client, _settings(tmp_path))
    query = FakeCallbackQuery(f"fb:down:{KN}")
    await _callback_handler(router, "feedback")(query)

    assert query.answered == [FEEDBACK_FAILED_TEXT]
    assert query.message.markup_removed is False


async def test_feedback_garbage_data_ignored(tmp_path):
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    for garbage in ("fb:sideways:50:21:0030210:123", "fb:up:мусор", "other:up:x"):
        query = FakeCallbackQuery(garbage)
        await _callback_handler(router, "feedback")(query)
        assert query.answered == [None]
    assert client.feedback_calls == []


async def test_listing_url_accepted_and_report_delivered(tmp_path):
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(LISTING_URL)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)

    assert client.listing_calls == [LISTING_URL]
    assert client.calls == []
    assert message.answers[0] == LISTING_ACCEPTED_TEXT
    resolution_note = message.answers[1]
    assert "50:28:0040126:62" in resolution_note
    assert "Барыбино" in resolution_note
    assert "не совпадает с данными реестра" in resolution_note
    assert "ранее учтённый" in resolution_note
    document, caption = message.documents[0]
    assert document.filename == "TerraLogicX_50-28-0040126-62.pdf"
    assert caption == "Экспресс-оценка участка\n50:28:0040126:62"


async def test_listing_ack_failure_does_not_kill_report_job(tmp_path):
    """Сбой отправки ack (сеть до Telegram) не отменяет формирование отчёта."""
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    message = FlakyAckMessage(LISTING_URL)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)

    assert client.listing_calls == [LISTING_URL]
    assert LISTING_ACCEPTED_TEXT not in message.answers
    resolution_note = message.answers[0]
    assert "50:28:0040126:62" in resolution_note
    document, caption = message.documents[0]
    assert document.filename == "TerraLogicX_50-28-0040126-62.pdf"
    assert caption == "Экспресс-оценка участка\n50:28:0040126:62"


async def test_cadastral_ack_failure_does_not_kill_report_job(tmp_path):
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    message = FlakyAckMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)

    assert client.calls == [KN]
    document, caption = message.documents[0]
    assert document.filename == "TerraLogicX_50-21-0030210-123.pdf"
    assert caption == f"Экспресс-оценка участка\n{KN}"


async def test_listing_url_inside_text(tmp_path):
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(f"Оцени участок: {LISTING_URL}")
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)
    assert client.listing_calls == [LISTING_URL]


async def test_listing_resolution_failed_message(tmp_path):
    client = FakeClient()
    client.listing_error = ListingResolutionFailed("no_landplots_at_coordinates")
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(LISTING_URL)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.answers) == 2)
    assert message.answers[-1] == LISTING_FAILED_TEXT
    assert message.documents == []


async def test_listing_resolution_failed_lists_nearby_parcels(tmp_path):
    """При приблизительной метке бот показывает участки рядом с ней."""
    client = FakeClient()
    client.listing_error = ListingResolutionFailed(
        "no_landplots_at_coordinates",
        nearby=[
            {
                "cadastral_number": "50:13:0020304:162",
                "distance_m": 100.0,
                "area_m2": 485.0,
            },
            {"cadastral_number": "50:13:0020303:260", "distance_m": 100.0},
        ],
    )
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(LISTING_URL)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.answers) == 2)
    answer = message.answers[-1]
    assert "приблизительная" in answer
    assert "50:13:0020304:162" in answer
    assert "4.85 сот." in answer
    assert "50:13:0020303:260" in answer
    assert message.documents == []


COORDS = "55.488626, 38.009447"


async def test_coords_message_produces_report(tmp_path):
    """Координаты «lat, lon» запускают coords-контур и отдают PDF."""
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(COORDS)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)
    assert client.coords_calls == [COORDS]
    assert client.calls == []
    assert client.listing_calls == []
    note = message.answers[1]
    assert note.startswith("По координатам найден участок 50:23:0000000:120934.")


async def test_coords_resolution_failed_lists_nearby_parcels(tmp_path):
    """Пустая точка: бот объясняет и перечисляет участки рядом."""
    client = FakeClient()
    client.coords_error = ListingResolutionFailed(
        "no_landplots_at_coordinates",
        nearby=[
            {
                "cadastral_number": "50:23:0000000:120935",
                "distance_m": 100.0,
                "area_m2": 900.0,
            },
        ],
    )
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(COORDS)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.answers) == 2)
    answer = message.answers[-1]
    assert "В указанной точке нет зарегистрированного участка" in answer
    assert "50:23:0000000:120935" in answer
    assert "9 сот." in answer
    assert message.documents == []


async def test_coords_resolution_failed_without_nearby(tmp_path):
    """Пустая точка без nearby — общий текст про координаты."""
    client = FakeClient()
    client.coords_error = ListingResolutionFailed("no cadastral candidates")
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(COORDS)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.answers) == 2)
    assert "Не удалось определить участок по координатам" in message.answers[-1]
    assert message.documents == []


async def test_cadastral_number_preferred_over_listing_url(tmp_path):
    """Номер и ссылка в одном сообщении — обрабатываем явный номер."""
    client = FakeClient()
    router = build_router(client, _settings(tmp_path))
    message = FakeMessage(f"{KN} {LISTING_URL}")
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)
    assert client.calls == [KN]
    assert client.listing_calls == []


async def test_listing_duplicate_active_request_rejected(tmp_path):
    client = FakeClient()
    client.gate = asyncio.Event()
    router = build_router(client, _settings(tmp_path))
    first = FakeMessage(LISTING_URL)
    second = FakeMessage(LISTING_URL, user_id=42)
    await _handler(router, "cadastral")(first)
    await _wait_for(lambda: client.active == 1)
    await _handler(router, "cadastral")(second)
    assert second.answers == [DUPLICATE_TEXT]
    client.gate.set()
    await _wait_for(lambda: len(first.documents) == 1)


async def test_report_limit_blocks_request(tmp_path):
    """Лимит исчерпан: задача не запускается, backend не вызывается."""
    quota = QuotaStore(":memory:")
    for _ in range(3):
        quota.record(42)
    client = FakeClient()
    router = build_router(client, _settings(tmp_path), quota)
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    assert len(message.answers) == 1
    assert "временно недоступна" in message.answers[0]
    assert client.calls == []
    assert message.documents == []


# ---------------------------------------------------------------------------
# Оплата пакетов (Robokassa)
# ---------------------------------------------------------------------------


def _paid_settings(tmp_path: Path) -> Settings:
    return replace(
        _settings(tmp_path),
        robokassa_merchant_login="demo",
        robokassa_password1="p1",
        robokassa_password2="p2",
        robokassa_test=True,
    )


def _exhausted_quota() -> QuotaStore:
    quota = QuotaStore(":memory:")
    for _ in range(3):
        quota.record(42)
    return quota


async def test_paywall_offers_packages_when_payments_enabled(tmp_path):
    """Первому покупателю paywall показывает промо-текст с пакетами."""
    quota = _exhausted_quota()
    router = build_router(
        FakeClient(), _paid_settings(tmp_path), quota, payment_checker=None
    )
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    assert len(message.answers) == 1
    assert "Первый отчёт" in message.answers[0]
    assert "промо-цене" in message.answers[0]


async def test_paywall_keyboard_lists_packages(tmp_path):
    """Первому покупателю paywall предлагает промо-цену 500 ₽."""
    quota = _exhausted_quota()
    router = build_router(FakeClient(), _paid_settings(tmp_path), quota)
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    keyboard = message.markups[0]
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert any("1 отчёт" in text and "500" in text for text in texts)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "pay:buy:report1" in callbacks


async def test_paywall_offers_full_price_after_first_purchase(tmp_path):
    """После первой оплаченной покупки цена — базовая 3000 ₽."""
    quota = _exhausted_quota()
    inv_id = quota.create_payment(
        42, package_code="report1", reports=1, amount_rub=500.0
    )
    quota.mark_payment_paid(inv_id)
    assert quota.consume(42, 3) == "paid"  # кредит израсходован → снова paywall
    router = build_router(FakeClient(), _paid_settings(tmp_path), quota)
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    assert "Каждый следующий отчёт" in message.answers[0]
    keyboard = message.markups[0]
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert any("1 отчёт" in text and "3000" in text for text in texts)


async def test_pay_buy_creates_invoice_with_payment_link(tmp_path):
    """Первая покупка выставляется по промо-цене 500 ₽."""
    quota = _exhausted_quota()
    router = build_router(FakeClient(), _paid_settings(tmp_path), quota)
    query = FakeCallbackQuery("pay:buy:report1", user_id=42)
    await _callback_handler(router, "pay_buy")(query)

    payment = quota.get_payment(1)
    assert payment is not None
    assert payment["user_id"] == 42
    assert payment["reports"] == 1
    assert payment["amount_rub"] == 500.0
    invoice_message = query.message
    assert invoice_message.answers  # текст счёта
    keyboard = invoice_message.markups[0]
    link_button = keyboard.inline_keyboard[0][0]
    assert link_button.url.startswith("https://auth.robokassa.ru/Merchant/Index.aspx?")
    assert "OutSum=500.00" in link_button.url
    assert "Shp_user_id=42" in link_button.url
    check_button = keyboard.inline_keyboard[1][0]
    assert check_button.callback_data == "pay:check:1"


async def test_pay_buy_second_purchase_uses_full_price(tmp_path):
    """После оплаченной покупки счёт выставляется по базовой цене 3000 ₽."""
    quota = _exhausted_quota()
    first_inv = quota.create_payment(
        42, package_code="report1", reports=1, amount_rub=500.0
    )
    quota.mark_payment_paid(first_inv)
    router = build_router(FakeClient(), _paid_settings(tmp_path), quota)
    query = FakeCallbackQuery("pay:buy:report1", user_id=42)
    await _callback_handler(router, "pay_buy")(query)

    payment = quota.get_payment(2)
    assert payment is not None
    assert payment["amount_rub"] == 3000.0
    link_button = query.message.markups[0].inline_keyboard[0][0]
    assert "OutSum=3000.00" in link_button.url


async def test_pay_check_paid_credits_reports(tmp_path):
    quota = _exhausted_quota()
    inv_id = quota.create_payment(
        42, package_code="report1", reports=1, amount_rub=3000.0
    )

    async def fake_checker(inv: int) -> str:
        return "paid"

    router = build_router(
        FakeClient(),
        _paid_settings(tmp_path),
        quota,
        payment_checker=fake_checker,
    )
    query = FakeCallbackQuery(f"pay:check:{inv_id}", user_id=42)
    await _callback_handler(router, "pay_check")(query)

    assert quota.credits(42) == 1
    assert quota.available(42, 3) == 1
    assert any("Оплата получена" in text for text in query.message.answers)
    # повторная проверка не начисляет дважды
    query2 = FakeCallbackQuery(f"pay:check:{inv_id}", user_id=42)
    await _callback_handler(router, "pay_check")(query2)
    assert quota.credits(42) == 1


async def test_pay_check_pending_keeps_waiting(tmp_path):
    quota = _exhausted_quota()
    inv_id = quota.create_payment(
        42, package_code="report1", reports=1, amount_rub=3000.0
    )

    async def fake_checker(inv: int) -> str:
        return "pending"

    router = build_router(
        FakeClient(),
        _paid_settings(tmp_path),
        quota,
        payment_checker=fake_checker,
    )
    query = FakeCallbackQuery(f"pay:check:{inv_id}", user_id=42)
    await _callback_handler(router, "pay_check")(query)

    assert quota.credits(42) == 0
    assert any("не подтверждён" in (text or "") for text in query.answered)


async def test_pay_check_rejects_foreign_invoice(tmp_path):
    quota = _exhausted_quota()
    inv_id = quota.create_payment(
        42, package_code="report1", reports=1, amount_rub=3000.0
    )

    async def fake_checker(inv: int) -> str:
        return "paid"

    router = build_router(
        FakeClient(),
        _paid_settings(tmp_path),
        quota,
        payment_checker=fake_checker,
    )
    query = FakeCallbackQuery(f"pay:check:{inv_id}", user_id=777)
    await _callback_handler(router, "pay_check")(query)

    assert quota.credits(42) == 0
    assert query.answered == [None]


async def test_paid_user_gets_report_after_purchase(tmp_path):
    """После покупки пакета запрос снова идёт в backend и списывает кредит."""
    quota = _exhausted_quota()
    inv_id = quota.create_payment(
        42, package_code="report1", reports=1, amount_rub=3000.0
    )
    quota.mark_payment_paid(inv_id)
    client = FakeClient()
    router = build_router(client, _paid_settings(tmp_path), quota)
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)
    assert client.calls == [KN]
    assert quota.credits(42) == 0
    assert quota.used(42) == 3  # бесплатный счётчик не тронут


async def test_successful_report_consumes_quota(tmp_path):
    """Успешно доставленный PDF списывает один отчёт из квоты."""
    quota = QuotaStore(":memory:")
    client = FakeClient()
    router = build_router(client, _settings(tmp_path), quota)
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)
    assert quota.used(42) == 1
    assert quota.remaining(42, 5) == 4


async def test_failed_report_does_not_consume_quota(tmp_path):
    """Ошибка сбора квоту не тратит."""
    quota = QuotaStore(":memory:")
    client = FakeClient()
    client.error = ParcelNotFound("нет такого")
    router = build_router(client, _settings(tmp_path), quota)
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.answers) == 2)
    assert quota.used(42) == 0


async def test_unlimited_user_bypasses_quota(tmp_path):
    """Пользователь из UNLIMITED_USER_IDS не ограничен."""
    quota = QuotaStore(":memory:")
    for _ in range(10):
        quota.record(42)
    client = FakeClient()
    settings = replace(_settings(tmp_path), unlimited_user_ids=frozenset({42}))
    router = build_router(client, settings, quota)
    message = FakeMessage(KN)
    await _handler(router, "cadastral")(message)
    await _wait_for(lambda: len(message.documents) == 1)
    assert len(client.calls) == 1


async def test_quota_is_checked_before_duplicate_guard(tmp_path):
    """Исчерпанный лимит отвечает одинаково и для повторного номера."""
    quota = QuotaStore(":memory:")
    for _ in range(5):
        quota.record(42)
    client = FakeClient()
    router = build_router(client, _settings(tmp_path), quota)
    for _ in range(2):
        message = FakeMessage(KN)
        await _handler(router, "cadastral")(message)
        assert len(message.answers) == 1
        assert "временно недоступна" in message.answers[0]


# --- прогресс запроса: статусное сообщение, тикер, очистка ---


def test_render_progress_shows_only_bar_and_digits():
    text = render_progress(38.4)
    assert "38%" in text
    assert "▓" in text and "░" in text
    # Никаких имён внутренних сервисов и этапов — только цифры.
    for marker in ("НСПД", "OSM", "РГИС", "geodocs", "этап"):
        assert marker not in text
    assert render_progress(0.0).endswith("0%")
    assert render_progress(100.0).endswith("100%")
    assert "▓" not in render_progress(0.0).splitlines()[-1]


class _SlowProgressClient:
    """Клиент с паузами между событиями — тикер успевает отрисоваться."""

    def __init__(self, percents: tuple[float, ...] = (10.0, 55.0, 100.0)) -> None:
        self._percents = percents

    async def prepare_quickreport(
        self, cadastral_number: str, *, progress_callback=None
    ) -> QuickReportResult:
        for percent in self._percents:
            if progress_callback is not None:
                progress_callback(percent)
            await asyncio.sleep(0.02)
        return QuickReportResult(
            cadastral_number=cadastral_number,
            case_id="case-50-21-0030210-123",
            collection_run_id="run-1",
            pdf_bytes=PDF_BYTES,
        )


async def test_progress_status_edited_monotonically_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr("terralogic_bot.handlers._PROGRESS_TICK_S", 0.005)
    message = FakeMessage(KN)
    await _report_job(
        message=message,
        client=_SlowProgressClient(),
        semaphore=asyncio.Semaphore(1),
        jobs={},
        key=(42, KN),
        request_id="progres1",
        cadastral_number=KN,
    )

    assert len(message.status_messages) == 1
    status = message.status_messages[0]
    assert status.deleted is True
    assert status.edits, "тикер не успел отрисовать ни одного процента"
    rendered = [int(edit.rsplit(" ", 1)[-1].rstrip("%")) for edit in status.edits]
    assert rendered == sorted(rendered)
    assert rendered[-1] == 100
    assert len(message.documents) == 1


async def test_progress_status_removed_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("terralogic_bot.handlers._PROGRESS_TICK_S", 0.005)
    client = FakeClient()
    client.error = TerraLogicUnavailable("backend down")
    message = FakeMessage(KN)
    await _report_job(
        message=message,
        client=client,
        semaphore=asyncio.Semaphore(1),
        jobs={},
        key=(42, KN),
        request_id="progres2",
        cadastral_number=KN,
    )

    assert message.answers[-1] == UNAVAILABLE_TEXT
    assert len(message.status_messages) == 1
    assert message.status_messages[0].deleted is True


async def test_progress_status_survives_send_failure(tmp_path, monkeypatch):
    """Сбой отправки статуса (как у ack) не мешает доставке отчёта."""
    monkeypatch.setattr("terralogic_bot.handlers._PROGRESS_TICK_S", 0.005)

    class _NoStatusMessage(FakeMessage):
        async def answer(self, text: str, **kwargs: object):
            if text.startswith("⏳"):
                raise TelegramNetworkError(
                    method=SendMessage(chat_id=self.from_user.id, text=text),
                    message="ClientConnectorError: Connection reset by peer",
                )
            return await super().answer(text, **kwargs)

    message = _NoStatusMessage(KN)
    await _report_job(
        message=message,
        client=_SlowProgressClient(),
        semaphore=asyncio.Semaphore(1),
        jobs={},
        key=(42, KN),
        request_id="progres3",
        cadastral_number=KN,
    )

    assert message.status_messages == []
    assert len(message.documents) == 1
