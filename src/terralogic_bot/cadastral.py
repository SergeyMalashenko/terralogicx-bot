"""Детерминированный разбор кадастрового номера.

Зеркалит правило terralogic-engine (`CADASTRAL_NUMBER_PATTERN`):
четыре числовые части через двоеточие; whitespace игнорируется.
Существование участка здесь не проверяется.
"""

from __future__ import annotations

import re

_CADASTRAL_NUMBER_RE = re.compile(r"\d+:\d+:\d+:\d+")
_WHITESPACE_RE = re.compile(r"\s+")
_URL_TOKEN_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_AVITO_LISTING_RE = re.compile(
    r"^https?://(?:(?:www|m)\.)?avito\.ru/[^\s?#]*_\d+(?:[?#]\S*)?$",
    re.IGNORECASE,
)


def parse_cadastral_number(text: str) -> str | None:
    """Канонический номер из пользовательского ввода или None."""
    if not text:
        return None
    normalized = _WHITESPACE_RE.sub("", text)
    if not _CADASTRAL_NUMBER_RE.fullmatch(normalized):
        return None
    return normalized


def parse_listing_url(text: str) -> str | None:
    """Ссылка на объявление Авито из пользовательского ввода или None.

    Резолюция ссылки выполняется на сервере (контур parcel-resolver);
    здесь только детерминированная проверка формы URL.
    """
    if not text:
        return None
    for match in _URL_TOKEN_RE.finditer(text):
        token = match.group(0).rstrip(".,;)!")
        if _AVITO_LISTING_RE.fullmatch(token):
            return token
    return None


def extract_cadastral_number(text: str) -> str | None:
    """Первый номер по шаблону в произвольном тексте (для разбора «КН + ссылка»).

    В отличие от строгого parse_cadastral_number — поиск, а не fullmatch:
    используется только когда в сообщении есть и ссылка, и явный номер.
    """
    if not text:
        return None
    match = _CADASTRAL_NUMBER_RE.search(text)
    return match.group(0) if match else None
