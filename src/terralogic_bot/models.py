"""Результат сценария: готовый QuickReport PDF для отправки."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class QuickReportResult(BaseModel):
    """Минимальный контракт для delivery: идентификаторы + байты PDF."""

    cadastral_number: str
    case_id: str
    collection_run_id: str
    pdf_bytes: bytes


class SourceStatus(BaseModel):
    """Статус одного источника данных из сводки engine."""

    model_config = ConfigDict(extra="ignore")

    source: str
    status: str
    detail: str | None = None


class DocumentStatus(BaseModel):
    """Статус одного нормативного документа из сводки engine."""

    model_config = ConfigDict(extra="ignore")

    doc_type: str | None = None
    number: str | None = None
    version_date: str | None = None
    municipality: str | None = None
    status: str
    reason: str | None = None


class CollectionStatus(BaseModel):
    """Сводка сбора по существующему кейсу (команда /status)."""

    case_id: str
    collection_run_id: str
    collection_status: str
    reused_collection: bool = False
    duration_s: float | None = None
    sources: list[SourceStatus] = []
    documents: list[DocumentStatus] = []
