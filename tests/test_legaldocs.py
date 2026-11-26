"""Версия оферты и конвертер Markdown → telegra.ph для юридических документов."""

from __future__ import annotations

from pathlib import Path

from terralogic_bot.config import Settings
from terralogic_bot.legaldocs import (
    FALLBACK_OFFER_VERSION,
    extract_offer_version,
    load_offer_text,
    markdown_to_telegraph,
    resolve_offer_version,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OFFER_PATH = _REPO_ROOT / "assets" / "TerraLogicX_Public_Offer.md"
_PRIVACY_PATH = _REPO_ROOT / "assets" / "TerraLogicX_Privacy_Policy.md"


def _settings(**overrides) -> Settings:
    base = {
        "telegram_bot_token": "t",
        "terralogic_mcp_url": "http://127.0.0.1:8004/mcp",
        "request_timeout_s": 5.0,
        "max_concurrent_reports": 1,
        "log_level": "DEBUG",
    }
    base.update(overrides)
    return Settings(**base)


def test_extract_offer_version_from_revision_line():
    assert extract_offer_version("# Оферта\n\nРедакция от 26.11.2026\n") == "2026-11-26"
    assert extract_offer_version("Редакция от 1.2.2027") == "2027-02-01"
    assert extract_offer_version("без строки редакции") is None


def test_resolve_offer_version_prefers_env(tmp_path):
    offer = tmp_path / "offer.md"
    offer.write_text("Редакция от 26.11.2026", encoding="utf-8")
    settings = _settings(offer_version="2099-01-01", offer_text_path=str(offer))
    assert resolve_offer_version(settings) == "2099-01-01"


def test_resolve_offer_version_from_file(tmp_path):
    offer = tmp_path / "offer.md"
    offer.write_text("Редакция от 26.11.2026", encoding="utf-8")
    settings = _settings(offer_text_path=str(offer))
    assert resolve_offer_version(settings) == "2026-11-26"
    assert load_offer_text(settings) == "Редакция от 26.11.2026"


def test_resolve_offer_version_fallback_when_file_missing(tmp_path):
    settings = _settings(offer_text_path=str(tmp_path / "missing.md"))
    assert resolve_offer_version(settings) == FALLBACK_OFFER_VERSION
    assert load_offer_text(settings) is None


def test_repo_offer_file_matches_default_version():
    text = _OFFER_PATH.read_text(encoding="utf-8")
    assert extract_offer_version(text) == "2026-11-26"


def test_markdown_to_telegraph_blocks_and_inline():
    nodes = markdown_to_telegraph(
        "# Заголовок\n\n"
        "Параграф с **жирным** и [ссылкой](https://example.ru).\n\n"
        "- один\n- два\n\n"
        "1. первый\n2. второй\n"
    )
    assert nodes[0] == {"tag": "h3", "children": ["Заголовок"]}
    paragraph = nodes[1]
    assert paragraph["tag"] == "p"
    assert {"tag": "b", "children": ["жирным"]} in paragraph["children"]
    assert {
        "tag": "a",
        "attrs": {"href": "https://example.ru"},
        "children": ["ссылкой"],
    } in paragraph["children"]
    assert nodes[2]["tag"] == "ul"
    assert [item["children"] for item in nodes[2]["children"]] == [["один"], ["два"]]
    assert nodes[3]["tag"] == "ol"
    assert [item["children"] for item in nodes[3]["children"]] == [
        ["первый"],
        ["второй"],
    ]


def test_markdown_to_telegraph_table_becomes_pre():
    nodes = markdown_to_telegraph(
        "| Цель | Данные |\n|---|---|\n| Заказ | Номер |\n"
    )
    assert len(nodes) == 1
    assert nodes[0]["tag"] == "pre"
    assert "Заказ" in nodes[0]["children"][0]
    assert "---" not in nodes[0]["children"][0]


def test_real_documents_convert_cleanly():
    """Оба опубликованных документа конвертируются без потерь блоков."""
    for path in (_OFFER_PATH, _PRIVACY_PATH):
        nodes = markdown_to_telegraph(path.read_text(encoding="utf-8"))
        assert len(nodes) > 10, f"{path.name}: подозрительно мало узлов"
        assert nodes[0]["tag"] == "h3"
        for node in nodes:
            assert node["tag"] in {"h3", "h4", "p", "ul", "ol", "pre"}
            assert node["children"], f"{path.name}: пустой узел {node['tag']}"
