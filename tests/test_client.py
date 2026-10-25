"""Тесты McpTerraLogicClient с fake транспортом (без реального MCP)."""

from __future__ import annotations

from pathlib import Path

import pytest

from terralogic_bot.client import McpTerraLogicClient
from terralogic_bot.errors import (
    InvalidCadastralNumber,
    ParcelNotFound,
    ReportGenerationFailed,
    SourceUnavailable,
    TerraLogicUnavailable,
    UnexpectedError,
)

CASE_ID = "case-50-21-0030210-123"
RUN_ID = "run-abc123"
PDF_REL = "reports/quickreport-run-abc.pdf"


def _store_with_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "cases" / CASE_ID / PDF_REL
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")
    return tmp_path


def _envelope(data: dict) -> dict:
    return {"ok": True, "data": data}


def _error(code: str, message: str = "boom", retryable: bool = False) -> dict:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def _client(tmp_path: Path, call_tool) -> McpTerraLogicClient:
    return McpTerraLogicClient(
        "http://127.0.0.1:8004/mcp",
        case_store_path=tmp_path,
        call_tool=call_tool,
    )


async def test_happy_path_calls_both_tools_and_resolves_pdf(tmp_path):
    store = _store_with_pdf(tmp_path)
    calls: list[tuple[str, dict]] = []

    async def fake_call(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "terralogic_prepare_case":
            return _envelope({"case_id": CASE_ID, "collection_run_id": RUN_ID})
        return _envelope({"pdf_relative_path": PDF_REL})

    client = _client(store, fake_call)
    result = await client.prepare_quickreport("50:21:0030210:123")

    assert calls[0] == (
        "terralogic_prepare_case",
        {"cadastral_number": "50:21:0030210:123"},
    )
    assert calls[1] == (
        "terralogic_prepare_quickreport",
        {"case_id": CASE_ID, "collection_run_id": RUN_ID},
    )
    assert result.case_id == CASE_ID
    assert result.collection_run_id == RUN_ID
    assert result.pdf_path.is_file()
    assert result.pdf_path == store / "cases" / CASE_ID / PDF_REL


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("invalid_request", InvalidCadastralNumber),
        ("not_found", ParcelNotFound),
        ("collection_failed", SourceUnavailable),
        ("internal_error", ReportGenerationFailed),
        ("unknown_code", UnexpectedError),
    ],
)
async def test_error_envelope_mapping(tmp_path, code, expected):
    async def fake_call(name, arguments):
        return _error(code)

    client = _client(tmp_path, fake_call)
    with pytest.raises(expected):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_transport_error_maps_to_unavailable(tmp_path):
    async def fake_call(name, arguments):
        raise ConnectionError("refused")

    client = _client(tmp_path, fake_call)
    with pytest.raises(TerraLogicUnavailable):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_timeout_maps_to_unavailable(tmp_path):
    async def fake_call(name, arguments):
        raise TimeoutError("read timeout")

    client = _client(tmp_path, fake_call)
    with pytest.raises(TerraLogicUnavailable):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_missing_pdf_in_response(tmp_path):
    async def fake_call(name, arguments):
        if name == "terralogic_prepare_case":
            return _envelope({"case_id": CASE_ID, "collection_run_id": RUN_ID})
        return _envelope({"pdf_relative_path": None})

    client = _client(tmp_path, fake_call)
    with pytest.raises(ReportGenerationFailed):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_pdf_file_absent_on_disk(tmp_path):
    async def fake_call(name, arguments):
        if name == "terralogic_prepare_case":
            return _envelope({"case_id": CASE_ID, "collection_run_id": RUN_ID})
        return _envelope({"pdf_relative_path": PDF_REL})

    client = _client(tmp_path, fake_call)  # файл не создан
    with pytest.raises(ReportGenerationFailed):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_incomplete_case_result(tmp_path):
    async def fake_call(name, arguments):
        return _envelope({"case_id": "", "collection_run_id": ""})

    client = _client(tmp_path, fake_call)
    with pytest.raises(UnexpectedError):
        await client.prepare_quickreport("50:21:0030210:123")
