"""Учёт квоты отчётов (QuotaStore) и конфигурация лимитов."""

from __future__ import annotations

from terralogic_bot.config import Settings
from terralogic_bot.quota import QuotaStore


def test_quota_counts_delivered_reports(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3")
    assert store.used(42) == 0
    assert store.remaining(42, 5) == 5
    assert store.record(42) == 1
    assert store.record(42) == 2
    assert store.remaining(42, 5) == 3
    store.close()


def test_quota_persists_across_reopen(tmp_path):
    path = tmp_path / "quota.sqlite3"
    store = QuotaStore(path)
    store.record(42)
    store.close()
    reopened = QuotaStore(path)
    assert reopened.used(42) == 1
    reopened.close()


def test_quota_remaining_never_negative():
    store = QuotaStore(":memory:")
    for _ in range(7):
        store.record(42)
    assert store.remaining(42, 5) == 0
    store.close()


def test_quota_is_per_user():
    store = QuotaStore(":memory:")
    store.record(42)
    store.record(42)
    store.record(7)
    assert store.used(42) == 2
    assert store.used(7) == 1
    assert store.used(100500) == 0
    store.close()


def test_settings_unlimited_users_from_env():
    env = {
        "TELEGRAM_BOT_TOKEN": "t",
        "UNLIMITED_USER_IDS": "42, 100500",
        "REPORT_LIMIT": "3",
    }
    settings = Settings.from_env(env)
    assert settings.report_limit == 3
    assert settings.unlimited_user_ids == frozenset({42, 100500})


def test_settings_quota_defaults():
    settings = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t"})
    assert settings.report_limit == 5
    assert settings.unlimited_user_ids == frozenset()
    assert settings.quota_db_path == "data/quota.sqlite3"
