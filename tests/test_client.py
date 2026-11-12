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


def _error(
    code: str,
    message: str = "boom",
    retryable: bool = False,
    details: dict | None = None,
) -> dict:
    error: dict = {"code": code, "message": message, "retryable": retryable}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}


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


async def test_submit_feedback_calls_tool_with_vote():
    calls: list[tuple[str, dict]] = []

    async def fake_call(name, arguments):
        calls.append((name, dict(arguments)))
        return _envelope(
            {
                "cadastral_number": "50:21:0030210:123",
                "vote": "up",
                "case_id": CASE_ID,
            }
        )

    client = _client(fake_call)
    await client.submit_feedback("50:21:0030210:123", "up", user_id=777)

    assert calls == [
        (
            "terralogic_submit_feedback",
            {
                "cadastral_number": "50:21:0030210:123",
                "vote": "up",
                "user_id": 777,
            },
        )
    ]


async def test_submit_feedback_error_mapping():
    async def fake_call(name, arguments):
        return _error("internal_error")

    client = _client(fake_call)
    with pytest.raises(ReportGenerationFailed):
        await client.submit_feedback("50:21:0030210:123", "down")


LISTING_URL = (
    "https://www.avito.ru/domodedovo/zemelnye_uchastki/"
    "uchastok_10_sot._izhs_8110049401"
)


def _fake_listing_backend(pdf_content: str | None = None):
    if pdf_content is None:
        pdf_content = base64.b64encode(PDF_BYTES).decode("ascii")
    calls: list[tuple[str, dict]] = []

    async def fake_call(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "terralogic_prepare_case_from_listing":
            return _envelope(
                {
                    "resolution": {
                        "url": arguments["url"],
                        "cadastral_number": "50:28:0040126:62",
                        "title": "Участок 10 сот. (ИЖС)",
                        "address": "МО, г. Домодедово, мкр-н Барыбино",
                        "warnings": ["area_mismatch: ..."],
                    },
                    "case": {"case_id": CASE_ID, "collection_run_id": RUN_ID},
                }
            )
        if name == "terralogic_prepare_quickreport":
            return _envelope({"report_id": "report-1"})
        return _envelope({"content_base64": pdf_content})

    return calls, fake_call


async def test_listing_happy_path_returns_pdf_and_resolution():
    calls, fake_call = _fake_listing_backend()
    client = _client(fake_call)
    result = await client.prepare_quickreport_from_listing(LISTING_URL)

    assert calls[0] == (
        "terralogic_prepare_case_from_listing",
        {"url": LISTING_URL},
    )
    assert calls[1] == (
        "terralogic_prepare_quickreport",
        {"case_id": CASE_ID, "collection_run_id": RUN_ID},
    )
    assert calls[2] == (
        "terralogic_get_report_pdf",
        {"case_id": CASE_ID, "collection_run_id": RUN_ID},
    )
    assert result.cadastral_number == "50:28:0040126:62"
    assert result.pdf_bytes == PDF_BYTES
    assert result.resolution is not None
    assert result.resolution.url == LISTING_URL
    assert result.resolution.title == "Участок 10 сот. (ИЖС)"
    assert result.resolution.warnings == ["area_mismatch: ..."]


COORDS = "55.488626, 38.009447"


def _fake_coords_backend():
    calls: list[tuple[str, dict]] = []

    async def fake_call(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "terralogic_prepare_case_at_coords":
            return _envelope(
                {
                    "resolution": {
                        "source": "coordinates",
                        "url": arguments["coordinates"],
                        "cadastral_number": "50:23:0000000:120934",
                        "latitude": 55.488626,
                        "longitude": 38.009447,
                        "warnings": [],
                    },
                    "case": {"case_id": CASE_ID, "collection_run_id": RUN_ID},
                }
            )
        if name == "terralogic_prepare_quickreport":
            return _envelope({"report_id": "report-1"})
        return _envelope({"content_base64": base64.b64encode(PDF_BYTES).decode()})

    return calls, fake_call


async def test_coords_happy_path_returns_pdf_and_resolution():
    calls, fake_call = _fake_coords_backend()
    client = _client(fake_call)
    result = await client.prepare_quickreport_at_coords(COORDS)

    assert calls[0] == (
        "terralogic_prepare_case_at_coords",
        {"coordinates": COORDS},
    )
    assert result.cadastral_number == "50:23:0000000:120934"
    assert result.pdf_bytes == PDF_BYTES
    assert result.resolution is not None
    assert result.resolution.source == "coordinates"
    assert result.resolution.url == COORDS
    assert result.resolution.title is None


async def test_coords_incomplete_payload_is_unexpected_error():
    async def fake_call(name, arguments):
        return _envelope({"resolution": {}, "case": {}})

    client = _client(fake_call)
    with pytest.raises(UnexpectedError):
        await client.prepare_quickreport_at_coords(COORDS)


async def test_listing_resolution_failed_maps_to_dedicated_error():
    from terralogic_bot.errors import ListingResolutionFailed

    async def fake_call(name, arguments):
        return _error(
            "listing_resolution_failed",
            "no_landplots_at_coordinates: the pin may be approximate",
        )

    client = _client(fake_call)
    with pytest.raises(ListingResolutionFailed, match="no_landplots"):
        await client.prepare_quickreport_from_listing(LISTING_URL)


async def test_listing_resolution_failed_carries_nearby_details():
    from terralogic_bot.errors import ListingResolutionFailed

    nearby = [
        {
            "cadastral_number": "50:13:0020304:162",
            "distance_m": 100.0,
            "area_m2": 485.0,
        },
        {"cadastral_number": "50:13:0020303:260", "distance_m": 100.0},
    ]

    async def fake_call(name, arguments):
        return _error(
            "listing_resolution_failed",
            "no cadastral candidates returned",
            details={"nearby": nearby},
        )

    client = _client(fake_call)
    with pytest.raises(ListingResolutionFailed) as excinfo:
        await client.prepare_quickreport_from_listing(LISTING_URL)
    assert excinfo.value.nearby == nearby


async def test_listing_incomplete_payload_is_unexpected_error():
    async def fake_call(name, arguments):
        return _envelope(
            {
                "resolution": {"cadastral_number": ""},
                "case": {"case_id": "", "collection_run_id": ""},
            }
        )

    client = _client(fake_call)
    with pytest.raises(UnexpectedError):
        await client.prepare_quickreport_from_listing(LISTING_URL)
