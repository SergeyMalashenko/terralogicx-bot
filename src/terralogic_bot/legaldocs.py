"""Юридические документы сервиса: версия оферты и публикация на telegra.ph.

Карточка заказа фиксирует версию редакции оферты, показанной пользователю,
а полный текст каждой редакции хранится в БД (quota.offer_versions) — так
условия конкретного заказа восстанавливаются в любой момент. Версия берётся
из OFFER_VERSION, а если переменная не задана — из строки «Редакция от
ДД.ММ.ГГГГ» в файле оферты.

markdown_to_telegraph — конвертер подмножества Markdown документов из
assets/ в дерево узлов telegra.ph (заголовки, параграфы, **жирный**,
[ссылки](url), списки; таблицы — моноширинным блоком pre).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import Settings

Node = dict[str, Any]

_VERSION_RE = re.compile(r"Редакция от\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")

#: Версия по умолчанию, если редакцию не удалось извлечь из файла оферты.
FALLBACK_OFFER_VERSION = "2026-11-26"


def extract_offer_version(text: str) -> str | None:
    """«Редакция от 26.11.2026» → «2026-11-26»; без строки редакции → None."""
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    day, month, year = (int(group) for group in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def resolve_offer_version(settings: Settings) -> str:
    """Версия действующей редакции: env → строка редакции в файле → запасная."""
    if settings.offer_version:
        return settings.offer_version
    try:
        text = Path(settings.offer_text_path).read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_OFFER_VERSION
    return extract_offer_version(text) or FALLBACK_OFFER_VERSION


def load_offer_text(settings: Settings) -> str | None:
    """Полный текст действующей оферты (None, если файл недоступен)."""
    try:
        return Path(settings.offer_text_path).read_text(encoding="utf-8")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Markdown → telegra.ph
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_UL_ITEM_RE = re.compile(r"^[-*]\s+(.*)$")
_OL_ITEM_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\|?[\s:|-]+\|?$")
_INLINE_TOKEN_RE = re.compile(r"(\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\))")
_LINK_RE = re.compile(r"^\[([^\]]+)\]\(([^)\s]+)\)$")
_BOLD_RE = re.compile(r"^\*\*([^*]+)\*\*$")


def _inline(text: str) -> list[str | Node]:
    """Строка → узлы текста/жирного/ссылки для children блочного узла."""
    children: list[str | Node] = []
    for chunk in _INLINE_TOKEN_RE.split(text):
        if not chunk:
            continue
        bold = _BOLD_RE.match(chunk)
        if bold is not None:
            children.append({"tag": "b", "children": [bold.group(1)]})
            continue
        link = _LINK_RE.match(chunk)
        if link is not None:
            children.append(
                {
                    "tag": "a",
                    "attrs": {"href": link.group(2)},
                    "children": [link.group(1)],
                }
            )
            continue
        children.append(chunk)
    return children


def _blocks(text: str) -> list[list[str]]:
    """Текст → блоки строк, разделённые пустыми строками."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line.rstrip())
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _list_node(tag: str, block: list[str], item_re: re.Pattern[str]) -> Node:
    """ul/ol-узел: каждая строка блока — элемент списка (проверено all())."""
    items = []
    for line in block:
        match = item_re.match(line)
        assert match is not None  # гарантировано проверкой all() вызывающего
        items.append({"tag": "li", "children": _inline(match.group(1).strip())})
    return {"tag": tag, "children": items}


def markdown_to_telegraph(text: str) -> list[Node]:
    """Markdown документов assets/ → узлы telegra.ph.

    Покрывается используемое подмножество: заголовки #..#### (→ h3/h4),
    параграфы, **жирный**, [ссылки](url), маркированные и нумерованные
    списки, таблицы (→ pre, без строк-разделителей).
    """
    nodes: list[Node] = []
    for block in _blocks(text):
        heading = _HEADING_RE.match(block[0])
        if heading is not None:
            level = len(heading.group(1))
            tag = "h3" if level <= 2 else "h4"
            nodes.append(
                {"tag": tag, "children": _inline(heading.group(2).strip())}
            )
            continue
        if all(line.lstrip().startswith("|") for line in block):
            rows = [
                line.strip().strip("|").strip()
                for line in block
                if not _TABLE_SEP_RE.match(line.strip())
            ]
            nodes.append({"tag": "pre", "children": ["\n".join(rows)]})
            continue
        if all(_UL_ITEM_RE.match(line) for line in block):
            nodes.append(_list_node("ul", block, _UL_ITEM_RE))
            continue
        if all(_OL_ITEM_RE.match(line) for line in block):
            nodes.append(_list_node("ol", block, _OL_ITEM_RE))
            continue
        nodes.append({"tag": "p", "children": _inline(" ".join(block))})
    return nodes
