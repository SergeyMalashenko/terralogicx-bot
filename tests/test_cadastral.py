"""Тесты детерминированного парсера кадастрового номера."""

from __future__ import annotations

from terralogic_bot.cadastral import parse_cadastral_number


def test_valid_number():
    assert parse_cadastral_number("50:21:0030210:123") == "50:21:0030210:123"


def test_surrounding_and_inner_whitespace():
    assert parse_cadastral_number("  50:21:0030210:123 \n") == "50:21:0030210:123"
    assert parse_cadastral_number("50: 21: 0030210: 123") == "50:21:0030210:123"


def test_empty_and_blank():
    assert parse_cadastral_number("") is None
    assert parse_cadastral_number("   ") is None


def test_missing_segment():
    assert parse_cadastral_number("50:21:0030210") is None


def test_extra_segment():
    assert parse_cadastral_number("50:21:0030210:123:7") is None


def test_non_numeric():
    assert parse_cadastral_number("50:21:0030210:12a") is None
    assert parse_cadastral_number("привет") is None


def test_other_separators_not_normalized():
    # fuzzy-коррекции нет: только двоеточие как разделитель
    assert parse_cadastral_number("50-21-0030210-123") is None
    assert parse_cadastral_number("50.21.0030210.123") is None


"""Тесты детерминированного парсера ссылки на объявление Авито."""

from terralogic_bot.cadastral import parse_listing_url

LISTING_URL = (
    "https://www.avito.ru/domodedovo/zemelnye_uchastki/"
    "uchastok_10_sot._izhs_8110049401"
)


def test_plain_listing_url():
    assert parse_listing_url(LISTING_URL) == LISTING_URL


def test_listing_url_with_context_param():
    url = LISTING_URL + "?context=H4sIAAAAAAAA"
    assert parse_listing_url(url) == url


def test_listing_url_inside_text():
    text = f"Смотри, какой участок: {LISTING_URL} — что скажешь?"
    assert parse_listing_url(text) == LISTING_URL


def test_mobile_listing_url():
    url = "https://m.avito.ru/domodedovo/zemelnye_uchastki/uchastok_6_sot_12345"
    assert parse_listing_url(url) == url


def test_trailing_punctuation_stripped():
    assert parse_listing_url(f"Привет! {LISTING_URL}.") == LISTING_URL


def test_non_listing_urls_rejected():
    assert parse_listing_url("https://www.avito.ru/domodedovo") is None
    assert parse_listing_url("https://www.cian.ru/sale/flat/123") is None
    assert parse_listing_url("https://avito.ru.evil.com/x_123") is None
    assert parse_listing_url("avito.ru/x_123") is None


def test_no_url_in_text():
    assert parse_listing_url("50:21:0030210:123") is None
    assert parse_listing_url("") is None
    assert parse_listing_url("привет") is None


from terralogic_bot.cadastral import extract_cadastral_number


def test_extract_number_from_mixed_text():
    assert extract_cadastral_number(f"50:21:0030210:123 {LISTING_URL}") == (
        "50:21:0030210:123"
    )


def test_extract_returns_none_without_number():
    assert extract_cadastral_number(LISTING_URL) is None
    assert extract_cadastral_number("") is None
