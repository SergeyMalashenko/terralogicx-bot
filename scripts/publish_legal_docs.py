#!/usr/bin/env python3
"""Публикация оферты и политики ПД на telegra.ph.

Первый запуск создаёт аккаунт telegra.ph и печатает access_token — сохраните
его в переменной окружения TELEGRAPH_ACCESS_TOKEN (или передавайте --token).
Повторные запуски обновляют те же страницы через edit_page, их URL остаются
постоянными; реестр страниц — data/telegraph_pages.json. Итоговые URL
прописываются в .env (OFFER_URL, PRIVACY_URL): их показывает карточка заказа.

В тексте оферты метка PRIVACY_URL заменяется адресом страницы политики,
поэтому политика публикуется первой.

Запуск: python scripts/publish_legal_docs.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from terralogic_bot.legaldocs import markdown_to_telegraph

_API = "https://api.telegra.ph"

DOCS = {
    "privacy": {
        "file": "assets/TerraLogicX_Privacy_Policy.md",
        "title": "Политика обработки персональных данных — TerraLogicX",
    },
    "offer": {
        "file": "assets/TerraLogicX_Public_Offer.md",
        "title": "Публичная оферта — TerraLogicX",
    },
}


async def _api(session: Any, method: str, **params: Any) -> dict[str, Any]:
    """Вызов telegra.ph API; не-ok ответ — исключение с пояснением."""
    async with session.post(f"{_API}/{method}", data=params) as response:
        payload = await response.json(content_type=None)
    if not payload.get("ok"):
        raise RuntimeError(f"telegra.ph {method} отклонён: {payload}")
    return payload["result"]


async def _upsert_page(
    session: Any,
    *,
    token: str,
    title: str,
    content_md: str,
    path: str | None,
) -> dict[str, str]:
    """Создать страницу или обновить существующую (path сохраняет URL)."""
    content = json.dumps(markdown_to_telegraph(content_md), ensure_ascii=False)
    params = {
        "access_token": token,
        "title": title,
        "content": content,
        "author_name": "TerraLogicX",
    }
    if path:
        result = await _api(session, f"editPage/{path}", **params)
    else:
        result = await _api(session, "createPage", **params)
    return {"path": result["path"], "url": result["url"]}


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=os.environ.get("TELEGRAPH_ACCESS_TOKEN"))
    parser.add_argument("--registry", default="data/telegraph_pages.json")
    parser.add_argument(
        "--account", default="TerraLogicX", help="short_name для нового аккаунта"
    )
    args = parser.parse_args()

    import aiohttp

    registry_path = Path(args.registry)
    registry: dict[str, dict[str, str]] = {}
    if registry_path.is_file():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))

    async with aiohttp.ClientSession() as session:
        token = args.token
        if not token:
            account = await _api(
                session,
                "createAccount",
                short_name=args.account,
                author_name="TerraLogicX",
            )
            token = account["access_token"]
            print(f"TELEGRAPH_ACCESS_TOKEN={token}  # сохраните в .env")

        # политика первой: оферта ссылается на её URL меткой PRIVACY_URL
        privacy = DOCS["privacy"]
        privacy_md = Path(privacy["file"]).read_text(encoding="utf-8")
        privacy_page = await _upsert_page(
            session,
            token=token,
            title=privacy["title"],
            content_md=privacy_md,
            path=registry.get("privacy", {}).get("path"),
        )
        registry["privacy"] = privacy_page

        offer = DOCS["offer"]
        offer_md = Path(offer["file"]).read_text(encoding="utf-8").replace(
            "PRIVACY_URL", privacy_page["url"]
        )
        offer_page = await _upsert_page(
            session,
            token=token,
            title=offer["title"],
            content_md=offer_md,
            path=registry.get("offer", {}).get("path"),
        )
        registry["offer"] = offer_page

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"OFFER_URL={registry['offer']['url']}")
    print(f"PRIVACY_URL={registry['privacy']['url']}")


if __name__ == "__main__":
    asyncio.run(_main())
