"""Результат сценария: готовый QuickReport в CaseStore."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class QuickReportResult(BaseModel):
    """Минимальный контракт для delivery: идентификаторы + путь к PDF."""

    cadastral_number: str
    case_id: str
    collection_run_id: str
    pdf_path: Path
