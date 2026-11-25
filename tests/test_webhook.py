"""Тесты ResultURL-приёмника Robokassa: подпись, зачисление, идемпотентность."""

from __future__ import annotations

import hashlib

import pytest
from aiohttp.test_utils import TestClient, TestServer

from terralogic_bot.quota import QuotaStore
from terralogic_bot.webhook import (
    RESULT_PATH,
    build_webhook_app,
    verify_result_signature,
)

PASSWORD2 = "test-pass-2"


def _signature(out_sum: str, inv_id: str, shp: dict[str, str] | None = None) -> str:
    base = ":".join(
        [out_sum, inv_id, PASSWORD2]
        + [f"{k}={v}" for k, v in sorted((shp or {}).items())]
    )
    return hashlib.md5(base.encode()).hexdigest()


def test_signature_ok() -> None:
    sig = _signature("3000.00", "42", {"Shp_user_id": "111"})
    assert verify_result_signature(
        out_sum="3000.00",
        inv_id="42",
        password2=PASSWORD2,
        signature=sig,
        shp={"Shp_user_id": "111"},
    )


def test_signature_uppercase_hex_accepted() -> None:
    sig = _signature("3000.00", "42").upper()
    assert verify_result_signature(
        out_sum="3000.00",
        inv_id="42",
        password2=PASSWORD2,
        signature=sig,
        shp={},
    )


def test_signature_rejects_tampering() -> None:
    sig = _signature("3000.00", "42", {"Shp_user_id": "111"})
    assert not verify_result_signature(
        out_sum="3000.00",
        inv_id="42",
        password2=PASSWORD2,
        signature=sig,
        shp={"Shp_user_id": "222"},
    )
    assert not verify_result_signature(
        out_sum="3000.01",
        inv_id="42",
        password2=PASSWORD2,
        signature=sig,
        shp={"Shp_user_id": "111"},
    )


@pytest.fixture
def quota() -> QuotaStore:
    store = QuotaStore(":memory:")
    yield store
    store.close()


async def _post(client: TestClient, payload: dict[str, str]) -> tuple[int, str]:
    response = await client.post(RESULT_PATH, data=payload)
    return response.status, await response.text()


def _payload(inv_id: int, user_id: int, out_sum: str = "3000.00") -> dict[str, str]:
    data = {
        "OutSum": out_sum,
        "InvId": str(inv_id),
        "Shp_user_id": str(user_id),
    }
    data["SignatureValue"] = _signature(
        out_sum, str(inv_id), {"Shp_user_id": str(user_id)}
    )
    return data


async def test_result_credits_reports_and_notifies(quota: QuotaStore) -> None:
    inv_id = quota.create_payment(
        111, package_code="report1", reports=1, amount_rub=3000.0
    )
    notified: list[tuple[int, int]] = []

    async def notify(user_id: int, reports: int) -> None:
        notified.append((user_id, reports))

    app = build_webhook_app(quota=quota, password2=PASSWORD2, notify=notify)
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(client, _payload(inv_id, 111))
    assert status == 200
    assert body == f"OK{inv_id}"
    assert quota.credits(111) == 1
    assert quota.get_payment(inv_id)["status"] == "paid"  # type: ignore[index]
    assert notified == [(111, 1)]


async def test_result_idempotent_second_call_notifies_once(
    quota: QuotaStore,
) -> None:
    inv_id = quota.create_payment(
        111, package_code="report1", reports=1, amount_rub=3000.0
    )
    calls = 0

    async def notify(user_id: int, reports: int) -> None:
        nonlocal calls
        calls += 1

    app = build_webhook_app(quota=quota, password2=PASSWORD2, notify=notify)
    async with TestClient(TestServer(app)) as client:
        for _ in range(2):
            status, body = await _post(client, _payload(inv_id, 111))
            assert status == 200
            assert body == f"OK{inv_id}"
    assert quota.credits(111) == 1  # не удвоилось
    assert calls == 1


async def test_result_rejects_bad_signature(quota: QuotaStore) -> None:
    inv_id = quota.create_payment(
        111, package_code="report1", reports=1, amount_rub=3000.0
    )
    payload = _payload(inv_id, 111)
    payload["SignatureValue"] = "0" * 32
    app = build_webhook_app(quota=quota, password2=PASSWORD2)
    async with TestClient(TestServer(app)) as client:
        status, _ = await _post(client, payload)
    assert status == 400
    assert quota.credits(111) == 0
    assert quota.get_payment(inv_id)["status"] == "pending"  # type: ignore[index]


async def test_result_rejects_unknown_invoice(quota: QuotaStore) -> None:
    app = build_webhook_app(quota=quota, password2=PASSWORD2)
    async with TestClient(TestServer(app)) as client:
        status, _ = await _post(client, _payload(9999, 111))
    assert status == 400


async def test_result_rejects_sum_mismatch(quota: QuotaStore) -> None:
    inv_id = quota.create_payment(
        111, package_code="report1", reports=1, amount_rub=3000.0
    )
    payload = _payload(inv_id, 111, out_sum="1.00")
    app = build_webhook_app(quota=quota, password2=PASSWORD2)
    async with TestClient(TestServer(app)) as client:
        status, _ = await _post(client, payload)
    assert status == 400
    assert quota.get_payment(inv_id)["status"] == "pending"  # type: ignore[index]


async def test_result_accepts_sum_without_decimals(quota: QuotaStore) -> None:
    """Robokassa шлёт OutSum=3000 вместо 3000.00 — это не mismatch."""
    inv_id = quota.create_payment(
        111, package_code="report1", reports=1, amount_rub=3000.0
    )
    app = build_webhook_app(quota=quota, password2=PASSWORD2)
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(client, _payload(inv_id, 111, out_sum="3000"))
    assert status == 200
    assert body == f"OK{inv_id}"
    assert quota.credits(111) == 1


async def test_result_rejects_malformed(quota: QuotaStore) -> None:
    app = build_webhook_app(quota=quota, password2=PASSWORD2)
    async with TestClient(TestServer(app)) as client:
        status, _ = await _post(client, {"OutSum": "3000.00"})
    assert status == 400


async def test_result_notify_failure_still_ok(quota: QuotaStore) -> None:
    inv_id = quota.create_payment(
        111, package_code="report1", reports=1, amount_rub=3000.0
    )

    async def notify(user_id: int, reports: int) -> None:
        raise RuntimeError("telegram недоступен")

    app = build_webhook_app(quota=quota, password2=PASSWORD2, notify=notify)
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(client, _payload(inv_id, 111))
    assert status == 200
    assert body == f"OK{inv_id}"
    assert quota.credits(111) == 1
