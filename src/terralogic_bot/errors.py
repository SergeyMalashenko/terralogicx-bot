"""Классификация ошибок приложения бота.

Небольшая плоская иерархия: handler сопоставляет каждому типу
понятное пользователю сообщение; traceback наружу не выходит.
"""

from __future__ import annotations


class BotError(Exception):
    """Базовая ожидаемая ошибка сценария бота."""


class InvalidCadastralNumber(BotError):
    """Формат кадастрового номера не распознан."""


class ParcelNotFound(BotError):
    """Источник однозначно сообщил, что участок не найден."""


class CaseNotCollected(BotError):
    """По этому номеру ещё нет собранного кейса (read-only /status)."""


class SourceUnavailable(BotError):
    """Временная недоступность одного из источников (retryable)."""


class TerraLogicUnavailable(BotError):
    """Сервис TerraLogicX недоступен (транспорт/таймаут)."""


class ReportGenerationFailed(BotError):
    """Отчёт не сформирован при рабочем backend."""


class UnexpectedError(BotError):
    """Непредвиденная ошибка (детали — только в лог)."""
