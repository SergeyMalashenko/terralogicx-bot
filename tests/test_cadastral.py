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
