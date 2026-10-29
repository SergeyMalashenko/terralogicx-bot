"""Тесты McpTerraLogicClient с fake транспортом (без реального MCP)."""

from __future__ import annotations

import base64

import pytest

from terralogic_bot.client import McpTerraLogicClient
from terralogic_bot.errors import (
    CaseNotCollected,
    InvalidCadastralNumber,
    ParcelNotFound,
    ReportGenerationFailed,
    SourceUnavailable,
    TerraLogicUnavailable,
    UnexpectedError,
)

CASE_ID = "case-50-21-0030210-123"
RUN_ID = "run-abc123"
PDF_BYTES = b"%PDF-1.4 fake"


def _envelope(data: dict) -> dict:
    return {"ok": True, "data": data}


def _error(code: str, message: str = "boom", retryable: bool = False) -> dict:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def _client(call_tool) -> McpTerraLogicClient:
    return McpTerraLogicClient(
        "http://127.0.0.1:8004/mcp",
        call_tool=call_tool,
    )


def _fake_backend(pdf_content: str | None = None):
    if pdf_content is None:
        pdf_content = base64.b64encode(PDF_BYTES).decode("ascii")
    calls: list[tuple[str, dict]] = []

    async def fake_call(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "terralogic_prepare_case":
            return _envelope({"case_id": CASE_ID, "collection_run_id": RUN_ID})
        if name == "terralogic_prepare_quickreport":
            return _envelope({"report_id": "report-1"})
        return _envelope({"content_base64": pdf_content})

    return calls, fake_call


async def test_happy_path_calls_three_tools_and_returns_pdf_bytes():
    calls, fake_call = _fake_backend()
    client = _client(fake_call)
    result = await client.prepare_quickreport("50:21:0030210:123")

    assert calls[0] == (
        "terralogic_prepare_case",
        {"cadastral_number": "50:21:0030210:123"},
    )
    assert calls[1] == (
        "terralogic_prepare_quickreport",
        {"case_id": CASE_ID, "collection_run_id": RUN_ID},
    )
    assert calls[2] == (
        "terralogic_get_report_pdf",
        {"case_id": CASE_ID, "collection_run_id": RUN_ID},
    )
    assert result.case_id == CASE_ID
    assert result.collection_run_id == RUN_ID
    assert result.pdf_bytes == PDF_BYTES


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("invalid_request", InvalidCadastralNumber),
        ("not_found", ParcelNotFound),
        ("not_collected", CaseNotCollected),
        ("collection_failed", SourceUnavailable),
        ("internal_error", ReportGenerationFailed),
        ("unknown_code", UnexpectedError),
    ],
)
async def test_error_envelope_mapping(code, expected):
    async def fake_call(name, arguments):
        return _error(code)

    client = _client(fake_call)
    with pytest.raises(expected):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_transport_error_maps_to_unavailable():
    async def fake_call(name, arguments):
        raise ConnectionError("refused")

    client = _client(fake_call)
    with pytest.raises(TerraLogicUnavailable):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_get_collection_status_uses_never_policy():
    calls: list[tuple[str, dict]] = []

    async def fake_call(name, arguments):
        calls.append((name, dict(arguments)))
        return _envelope(
            {
                "case_id": CASE_ID,
                "collection_run_id": RUN_ID,
                "collection_status": "partial",
                "reused_collection": True,
                "collection": {
                    "duration_s": 187.0,
                    "sources": [
                        {"source": "nspd", "status": "ok"},
                        {"source": "osm", "status": "failed", "detail": "timeout"},
                    ],
                    "documents": [
                        {
                            "doc_type": "pzz",
                            "number": "1749",
                            "version_date": "2026-05-15",
                            "status": "extracted",
                        }
                    ],
                },
            }
        )

    client = _client(fake_call)
    status = await client.get_collection_status("50:29:0030502:979")

    assert calls == [
        (
            "terralogic_prepare_case",
            {"cadastral_number": "50:29:0030502:979", "refresh_policy": "never"},
        )
    ]
    assert status.case_id == CASE_ID
    assert status.collection_status == "partial"
    assert status.reused_collection is True
    assert status.duration_s == 187.0
    assert status.sources[1].status == "failed"
    assert status.sources[1].detail == "timeout"
    assert status.documents[0].number == "1749"
    assert status.documents[0].status == "extracted"


async def test_timeout_maps_to_unavailable():
    async def fake_call(name, arguments):
        raise TimeoutError("read timeout")

    client = _client(fake_call)
    with pytest.raises(TerraLogicUnavailable):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_missing_pdf_content_in_response():
    _, fake_call = _fake_backend(pdf_content="")
    client = _client(fake_call)
    with pytest.raises(ReportGenerationFailed):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_corrupted_base64_payload():
    _, fake_call = _fake_backend(pdf_content="!!!not-base64!!!")
    client = _client(fake_call)
    with pytest.raises(ReportGenerationFailed):
        await client.prepare_quickreport("50:21:0030210:123")


async def test_incomplete_case_result():
    async def fake_call(name, arguments):
        return _envelope({"case_id": "", "collection_run_id": ""})

    client = _client(fake_call)
    with pytest.raises(UnexpectedError):
        await client.prepare_quickreport("50:21:0030210:123")
