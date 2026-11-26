"""Оплата пакетов отчётов через Robokassa: подписанная ссылка + опрос статуса.

Бот формирует ссылку на платёжную страницу (подпись MD5 с паролём №1 и
пользовательским параметром Shp_user_id) и сам опрашивает состояние счёта
через XML-интерфейс OpStateExt (подпись MD5 с паролём №2) — публичный
ResultURL не требуется. Считаются оплаченными состояния 50 («деньги
получены») и 100 («завершён»).
"""

from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from urllib.parse import urlencode

LOGGER = logging.getLogger("terralogic_bot")

_PAYMENT_PAGE_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
_OPSTATE_URL = "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"

# Состояния счёта Robokassa: 5 — создан, 10 — отменён,
# 50 — оплачен (деньги получены), 100 — завершён успешно.
_PAID_STATES = frozenset({50, 100})
_CANCELLED_STATES = frozenset({10})


class PaymentError(Exception):
    """Сбой обращения к Robokassa или неожиданный ответ."""


class PaymentRejected(PaymentError):
    """Robokassa отвергла запрос (неверная подпись, счёт не найден)."""


@dataclass(frozen=True)
class PaymentPackage:
    """Покупка на paywall-экране: сколько отчётов и за сколько."""

    code: str
    title: str
    reports: int
    price_rub: float

    @property
    def price_label(self) -> str:
        return f"{self.price_rub:g} ₽"


# Схема оплаты: бесплатных отчётов нет (REPORT_LIMIT=0); первая покупка —
# по промо-цене, последующие — по базовой.
FIRST_REPORT_PRICE_RUB = 500.0
REPORT_PRICE_RUB = 3000.0

PACKAGES: tuple[PaymentPackage, ...] = (
    PaymentPackage(
        code="report1", title="1 отчёт", reports=1, price_rub=REPORT_PRICE_RUB
    ),
)

_PACKAGES_BY_CODE = {package.code: package for package in PACKAGES}


def get_package(code: str) -> PaymentPackage | None:
    return _PACKAGES_BY_CODE.get(code)


def package_for_user(code: str, *, first_purchase: bool) -> PaymentPackage | None:
    """Пакет с ценой для конкретного пользователя: первая покупка — промо-цена."""
    package = get_package(code)
    if package is None:
        return None
    if first_purchase:
        return replace(package, price_rub=FIRST_REPORT_PRICE_RUB)
    return package


def build_payment_url(
    *,
    merchant_login: str,
    password1: str,
    inv_id: int,
    package: PaymentPackage,
    user_id: int,
    test: bool,
    description: str | None = None,
) -> str:
    """Подписанная ссылка на платёжную страницу Robokassa.

    Подпись: MD5(MerchantLogin:OutSum:InvId:Пароль#1:Shp_user_id=<id>).
    description — назначение платежа (видно плательщику и в чеке),
    Robokassa ограничивает его 100 символами.
    """
    out_sum = f"{package.price_rub:.2f}"
    shp = {"Shp_user_id": str(user_id)}
    signature_base = ":".join(
        [merchant_login, out_sum, str(inv_id), password1]
        + [f"{key}={value}" for key, value in sorted(shp.items())]
    )
    signature = hashlib.md5(signature_base.encode()).hexdigest()
    params = {
        "MerchantLogin": merchant_login,
        "OutSum": out_sum,
        "InvId": inv_id,
        "Description": (description or f"TerraLogicX: {package.title}")[:100],
        "SignatureValue": signature,
        "Culture": "ru",
        **shp,
    }
    if test:
        params["IsTest"] = "1"
    return f"{_PAYMENT_PAGE_URL}?{urlencode(params)}"


def parse_invoice_state(xml_payload: str) -> str:
    """Разбор ответа OpStateExt: 'paid' | 'pending' | 'cancelled'.

    Result/Code != 0 → PaymentRejected (подпись, права, нет счёта).
    """
    try:
        root = ET.fromstring(xml_payload)
    except ET.ParseError as exc:
        raise PaymentError("Robokassa вернула не XML") from exc

    def _code(parent_path: str) -> int:
        node = root.find(f".//{{*}}{parent_path}/{{*}}Code")
        if node is None or not (node.text or "").strip().isdigit():
            raise PaymentError(f"в ответе Robokassa нет кода {parent_path}")
        return int(node.text.strip())

    result_code = _code("Result")
    if result_code != 0:
        raise PaymentRejected(f"Robokassa отклонила запрос, код {result_code}")
    state_code = _code("State")
    if state_code in _PAID_STATES:
        return "paid"
    if state_code in _CANCELLED_STATES:
        return "cancelled"
    return "pending"


async def check_invoice(
    *,
    merchant_login: str,
    password2: str,
    inv_id: int,
    timeout_s: float = 20.0,
) -> str:
    """Опрос состояния счёта через OpStateExt (подпись по паролю №2)."""
    import aiohttp  # зависимость aiogram — всегда доступна в боте

    signature = hashlib.md5(
        f"{merchant_login}:{inv_id}:{password2}".encode()
    ).hexdigest()
    params = {
        "MerchantLogin": merchant_login,
        "InvId": inv_id,
        "Signature": signature,
    }
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_s)
            ) as session,
            session.get(_OPSTATE_URL, params=params) as response,
        ):
            payload = await response.text()
            if response.status != 200:
                raise PaymentError(f"OpStateExt вернул HTTP {response.status}")
    except PaymentError:
        raise
    except Exception as exc:  # сеть, таймаут, DNS
        raise PaymentError(f"OpStateExt недоступен: {exc}") from exc
    return parse_invoice_state(payload)
