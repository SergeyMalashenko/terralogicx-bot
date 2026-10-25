"""Клиент к публичному MCP API TerraLogicX (terralogic-mcp).

Единственная операция бота: prepare_quickreport(кадастровый номер) → PDF.
Внутри — два существующих инструмента engine: terralogic_prepare_case
(сбор + аналитика) и terralogic_prepare_quickreport (рендер отчёта).
Бот не импортирует internals terralogic-engine и не знает об LLM-контуре.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from .errors import (
    InvalidCadastralNumber,
    ParcelNotFound,
    ReportGenerationFailed,
    SourceUnavailable,
    TerraLogicUnavailable,
    UnexpectedError,
)
from .models import QuickReportResult

LOGGER = logging.getLogger(__name__)

CallToolFn = Callable[[str, Mapping[str, Any]], Awaitable[dict[str, Any]]]

_ERROR_MAP = {
    "invalid_request": InvalidCadastralNumber,
    "not_found": ParcelNotFound,
    "collection_failed": SourceUnavailable,
    "internal_error": ReportGenerationFailed,
}


class TerraLogicClient(Protocol):
    """Граница приложения: handlers зависят только от этого интерфейса."""

    async def prepare_quickreport(
        self, cadastral_number: str
    ) -> QuickReportResult: ...


def _group_leaves(exc: BaseExceptionGroup) -> list[BaseException]:
    leaves: list[BaseException] = []
    for sub in exc.exceptions:
        if isinstance(sub, BaseExceptionGroup):
            leaves.extend(_group_leaves(sub))
        else:
            leaves.append(sub)
    return leaves


class McpTerraLogicClient:
    """Streamable-http клиент terralogic-mcp на официальном MCP SDK."""

    def __init__(
        self,
        url: str,
        *,
        case_store_path: Path,
        read_timeout_s: float = 900.0,
        call_tool: CallToolFn | None = None,
    ) -> None:
        self._url = url
        self._case_store_path = case_store_path
        self._read_timeout_s = read_timeout_s
        self._call_tool_impl = call_tool or self._mcp_call_tool

    async def prepare_quickreport(self, cadastral_number: str) -> QuickReportResult:
        """Существующий use case TerraLogicX: сбор кейса → QuickReport → PDF."""
        case = await self._checked_call(
            "terralogic_prepare_case", {"cadastral_number": cadastral_number}
        )
        case_id = str(case.get("case_id") or "")
        run_id = str(case.get("collection_run_id") or "")
        if not case_id or not run_id:
            raise UnexpectedError("prepare_case вернул неполный результат")

        report = await self._checked_call(
            "terralogic_prepare_quickreport",
            {"case_id": case_id, "collection_run_id": run_id},
        )
        pdf_relative = report.get("pdf_relative_path")
        if not pdf_relative:
            raise ReportGenerationFailed("отчёт сформирован без PDF")
        pdf_path = self._case_store_path / "cases" / case_id / str(pdf_relative)
        if not pdf_path.is_file():
            raise ReportGenerationFailed(f"PDF не найден: {pdf_path}")
        return QuickReportResult(
            cadastral_number=cadastral_number,
            case_id=case_id,
            collection_run_id=run_id,
            pdf_path=pdf_path,
        )

    async def _checked_call(
        self, name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Вызов инструмента с маппингом конверта ToolResult на ошибки бота."""
        try:
            envelope = await self._call_tool_impl(name, arguments)
        except TimeoutError as exc:
            raise TerraLogicUnavailable(f"таймаут вызова {name}") from exc
        except TerraLogicUnavailable:
            raise
        except Exception as exc:
            raise TerraLogicUnavailable(f"транспорт MCP недоступен: {exc}") from exc
        if envelope.get("ok") is True and isinstance(envelope.get("data"), dict):
            return envelope["data"]
        error = envelope.get("error") or {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or "unknown error")
        LOGGER.warning("TerraLogic tool %s отклонён: %s — %s", name, code, message)
        error_type = _ERROR_MAP.get(code, UnexpectedError)
        raise error_type(message)

    async def _mcp_call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Реальный вызов MCP: session per call, structuredContent конверта."""
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        timeout = httpx.Timeout(self._read_timeout_s, connect=10.0)
        read_timeout = timedelta(seconds=self._read_timeout_s)
        try:
            async with (
                streamable_http_client(
                    self._url, http_client=httpx.AsyncClient(timeout=timeout)
                ) as streams,
                ClientSession(
                    streams[0], streams[1], read_timeout_seconds=read_timeout
                ) as session,
            ):
                await session.initialize()
                result = await session.call_tool(name, dict(arguments))
        except BaseExceptionGroup as exc:
            # anyio TaskGroup транспорта заворачивает сетевые сбои — расплющиваем
            leaves = "; ".join(str(leaf) for leaf in _group_leaves(exc))
            raise TerraLogicUnavailable(
                f"MCP tool {name!r} transport failed: {leaves or exc}"
            ) from exc
        if getattr(result, "isError", False):
            texts = [
                str(getattr(item, "text", ""))
                for item in (getattr(result, "content", None) or [])
            ]
            raise TerraLogicUnavailable(
                f"MCP tool {name!r} failed: {'; '.join(texts) or 'isError'}"
            )
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if text:
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    return payload
        raise TerraLogicUnavailable(
            f"MCP tool {name!r} вернул ответ без structured payload"
        )
