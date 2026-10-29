"""Тесты Telegram handlers с fake client и fake message (без Telegram API)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from terralogic_bot.config import Settings
from terralogic_bot.errors import (
    CaseNotCollected,
    ParcelNotFound,
    ReportGenerationFailed,
    TerraLogicUnavailable,
)
from terralogic_bot.handlers import (
    DUPLICATE_TEXT,
    FAILED_TEXT,
    HELP_TEXT,
    INVALID_TEXT,
    NOT_COLLECTED_TEXT,
    NOT_FOUND_TEXT,
    START_TEXT,
    STATUS_USAGE_TEXT,
    UNAVAILABLE_TEXT,
    build_router,
    report_filename,
)
from terralogic_bot.models import (
    CollectionStatus,
    DocumentStatus,
    QuickReportResult,
    SourceStatus,
)

KN = "50:21:0030210:123"


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeMessage:
    """Минимальный контракт aiogram Message, используемый handlers."""

    def __init__(self, text: str | None, user_id: int = 42) -> None:
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.answers: list[str] = []
        self.documents: list[tuple[object, str | None]] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append(text)

    async def answer_document(
        self, document: object, caption: str | None = None, **kwargs: object
    ) -> None:
        self.documents.append((document, caption))


PDF_BYTES = b"%PDF-1.4 canonical"


class FakeClient:
    """Контролируемый backend: задержка, счётчики вызовов и параллелизма."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.status_error: Exception | None = None
        self.status_payload: CollectionStatus | None = None

    async def prepare_quickreport(self, cadastral_number: str) -> QuickReportResult:
        self.calls.append(cadastral_number)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0.01)
            if self.error is not None:
                raise self.error
            return QuickReportResult(
                cadastral_number=cadastral_number,
                case_id="case-50-21-0030210-123",
                collection_run_id="run-1",
                pdf_bytes=PDF_BYTES,
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


def _settings(tmp_path: Path, max_concurrent: int = 2) -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        terralogic_mcp_url="http://127.0.0.1:8004/mcp",
        request_timeout_s=5.0,
        max_concurrent_reports=max_concurrent,
        log_level="DEBUG",
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
