"""Детерминированный разбор кадастрового номера.

Зеркалит правило terralogic-engine (`CADASTRAL_NUMBER_PATTERN`):
четыре числовые части через двоеточие; whitespace игнорируется.
Существование участка здесь не проверяется.
"""

from __future__ import annotations

import re

_CADASTRAL_NUMBER_RE = re.compile(r"\d+:\d+:\d+:\d+")
_WHITESPACE_RE = re.compile(r"\s+")


def parse_cadastral_number(text: str) -> str | None:
    """Канонический номер из пользовательского ввода или None."""
    if not text:
        return None
    normalized = _WHITESPACE_RE.sub("", text)
    if not _CADASTRAL_NUMBER_RE.fullmatch(normalized):
        return None
    return normalized
