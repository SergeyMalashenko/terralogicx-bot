"""Приём ResultURL-уведомлений Robokassa об оплате счетов.

Robokassa отправляет POST с OutSum, InvId, SignatureValue и возвращёнными
Shp_*-параметрами. Подпись считается как
MD5(OutSum:InvId:Пароль#2:Shp_* по алфавиту) — пароль №2 из того же блока
настроек (тестового или боевого), с которым выставлялся счёт. Успешный
ответ — «OK{InvId}»: без него Robokassa считает уведомление недоставленным.

Опрос OpStateExt остаётся запасным каналом: в тестовом режиме он платежей
не видит, поэтому webhook — единственный способ авто-подтверждения в тесте.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from .quota import QuotaStore

LOGGER = logging.getLogger("terralogic_bot")

RESULT_PATH = "/robokassa/result"

# notify(user_id, reports) — сообщить пользователю о зачислении пакета.
NotifyFn = Callable[[int, int], Awaitable[None]]


def verify_result_signature(
    *,
    out_sum: str,
    inv_id: str,
    password2: str,
    signature: str,
    shp: dict[str, str],
) -> bool:
    """Сверка подписи ResultURL (регистр hex-подписи не важен)."""
    base = ":".join(
        [out_sum, inv_id, password2]
        + [f"{key}={value}" for key, value in sorted(shp.items())]
    )
    expected = hashlib.md5(base.encode()).hexdigest()
    return expected.lower() == signature.strip().lower()


def build_webhook_app(
    *,
    quota: QuotaStore,
    password2: str,
    notify: NotifyFn | None = None,
) -> web.Application:
    """aiohttp-приложение с единственным endpoint'ом ResultURL."""

    async def result(request: web.Request) -> web.Response:
        data = await request.post()
        out_sum = (data.get("OutSum") or "").strip()
        inv_id_raw = (data.get("InvId") or "").strip()
        signature = (data.get("SignatureValue") or "").strip()
        shp = {
            key: value for key, value in data.items() if key.lower().startswith("shp_")
        }
        if not out_sum or not inv_id_raw.isdigit() or not signature:
            LOGGER.warning("robokassa result: malformed request: %s", dict(data))
            return web.Response(status=400, text="bad request")
        if not verify_result_signature(
            out_sum=out_sum,
            inv_id=inv_id_raw,
            password2=password2,
            signature=signature,
            shp=shp,
        ):
            LOGGER.warning("robokassa result: bad signature: inv=%s", inv_id_raw)
            return web.Response(status=400, text="bad signature")
        inv_id = int(inv_id_raw)
        payment = quota.get_payment(inv_id)
        if payment is None:
            LOGGER.warning("robokassa result: unknown invoice: inv=%s", inv_id)
            return web.Response(status=400, text="unknown invoice")
        try:
            received_sum = float(out_sum)
        except ValueError:
            LOGGER.warning(
                "robokassa result: bad sum format: inv=%s sum=%r", inv_id, out_sum
            )
            return web.Response(status=400, text="bad sum")
        expected_sum = float(payment["amount_rub"])
        # Robokassa может прислать «3000» вместо «3000.00» — сравниваем числа
        if abs(received_sum - expected_sum) > 0.005:
            LOGGER.warning(
                "robokassa result: sum mismatch: inv=%s got=%s expected=%s",
                inv_id,
                out_sum,
                expected_sum,
            )
            return web.Response(status=400, text="sum mismatch")
        paid = quota.mark_payment_paid(inv_id)
        if paid is None:
            LOGGER.info("robokassa result: already credited: inv=%s", inv_id)
        else:
            LOGGER.info(
                "robokassa result: paid: inv=%s user=%s reports=%s",
                inv_id,
                paid["user_id"],
                paid["reports"],
            )
            if notify is not None:
                try:
                    await notify(int(paid["user_id"]), int(paid["reports"]))
                except Exception:  # уведомление не должно ломать ответ Robokassa
                    LOGGER.exception("robokassa result: notify failed: inv=%s", inv_id)
        return web.Response(text=f"OK{inv_id}")

    app = web.Application()
    app.router.add_post(RESULT_PATH, result)
    return app


async def start_webhook(app: web.Application, *, host: str, port: int) -> web.AppRunner:
    """Поднять HTTP-сервер; вернуть runner для последующего cleanup()."""
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host=host, port=port).start()
    LOGGER.info("robokassa webhook listening: http://%s:%s%s", host, port, RESULT_PATH)
    return runner
