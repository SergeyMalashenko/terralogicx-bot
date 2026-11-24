"""Платёжный контур Robokassa: ссылка, подпись, разбор OpStateExt."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from terralogic_bot.payments import (
    PACKAGES,
    PaymentRejected,
    build_payment_url,
    get_package,
    parse_invoice_state,
)

PACKAGE = PACKAGES[0]


def _payment_url(**overrides):
    kwargs = {
        "merchant_login": "demo",
        "password1": "p1",
        "inv_id": 7,
        "package": PACKAGE,
        "user_id": 42,
        "test": True,
    }
    kwargs.update(overrides)
    return build_payment_url(**kwargs)


def test_payment_url_signature_matches_docs_formula():
    url = _payment_url()
    query = parse_qs(urlparse(url).query)
    expected = hashlib.md5(b"demo:3000.00:7:p1:Shp_user_id=42").hexdigest()
    assert query["SignatureValue"] == [expected]
    assert query["MerchantLogin"] == ["demo"]
    assert query["OutSum"] == ["3000.00"]
    assert query["InvId"] == ["7"]
    assert query["Shp_user_id"] == ["42"]
    assert query["IsTest"] == ["1"]


def test_payment_url_production_omits_istest():
    url = _payment_url(test=False)
    assert "IsTest" not in parse_qs(urlparse(url).query)


def test_payment_url_description_mentions_package():
    url = _payment_url()
    query = parse_qs(urlparse(url).query)
    assert PACKAGE.title in query["Description"][0]


def test_get_package_lookup():
    assert get_package("report1") is PACKAGES[0]
    assert get_package("nope") is None


_OPSTATE_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<OperationStateResponse xmlns="http://merchant.roboxchange.com/WebService/">
  <Result><Code>{result}</Code><Description></Description></Result>
  <State><Code>{state}</Code><RequestDate>2026-11-24T10:00:00</RequestDate></State>
</OperationStateResponse>
"""


@pytest.mark.parametrize(
    ("state", "expected"),
    [(5, "pending"), (50, "paid"), (100, "paid"), (10, "cancelled"), (60, "pending")],
)
def test_parse_invoice_state_mapping(state, expected):
    xml = _OPSTATE_TEMPLATE.format(result=0, state=state)
    assert parse_invoice_state(xml) == expected


def test_parse_invoice_state_rejected():
    xml = _OPSTATE_TEMPLATE.format(result=5, state=5)
    with pytest.raises(PaymentRejected):
        parse_invoice_state(xml)
