"""Результат сценария: готовый QuickReport PDF для отправки."""

from __future__ import annotations

from pydantic import BaseModel


class QuickReportResult(BaseModel):
    """Минимальный контракт для delivery: идентификаторы + байты PDF."""

    cadastral_number: str
    case_id: str
    collection_run_id: str
    pdf_bytes: bytes
