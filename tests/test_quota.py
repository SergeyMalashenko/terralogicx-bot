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
    assert settings.report_limit == 3
    assert settings.unlimited_user_ids == frozenset()
    assert settings.quota_db_path == "data/quota.sqlite3"


def test_touch_user_creates_profile():
    store = QuotaStore(":memory:")
    store.touch_user(42, username="sergey", first_name="Sergey", last_name="Malashenko")
    users = store.list_users()
    assert len(users) == 1
    user = users[0]
    assert user["user_id"] == 42
    assert user["username"] == "sergey"
    assert user["first_name"] == "Sergey"
    assert user["last_name"] == "Malashenko"
    assert user["first_seen_at"] == user["last_seen_at"]
    store.close()


def test_touch_user_updates_profile_and_keeps_first_seen():
    store = QuotaStore(":memory:")
    store.touch_user(42, username="old", first_name="Sergey")
    first_seen = store.list_users()[0]["first_seen_at"]
    store.touch_user(42, username="new", first_name="Sergey", last_name="M.")
    user = store.list_users()[0]
    assert user["username"] == "new"
    assert user["last_name"] == "M."
    assert user["first_seen_at"] == first_seen
    assert user["last_seen_at"] >= first_seen
    store.close()


def test_touch_user_without_optional_fields():
    store = QuotaStore(":memory:")
    store.touch_user(7)
    user = store.list_users()[0]
    assert user["username"] is None
    assert user["first_name"] is None
    store.close()


def test_users_registry_persists_across_reopen(tmp_path):
    path = tmp_path / "quota.sqlite3"
    store = QuotaStore(path)
    store.touch_user(42, username="sergey")
    store.close()
    reopened = QuotaStore(path)
    users = reopened.list_users()
    assert [u["user_id"] for u in users] == [42]
    assert users[0]["username"] == "sergey"
    reopened.close()


def test_remember_user_tolerates_store_failure():
    import sqlite3

    from terralogic_bot.quota import remember_user

    class _BrokenStore:
        def touch_user(self, *args, **kwargs):
            raise sqlite3.OperationalError("db is down")

    class _User:
        id = 42
        username = "u"
        first_name = "f"
        last_name = None

    remember_user(_BrokenStore(), _User())  # не падает
    remember_user(None, _User())  # quota не подключена
    remember_user(QuotaStore(":memory:"), None)  # анонимный апдейт


def test_payment_lifecycle(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3")
    inv1 = store.create_payment(
        42, package_code="report1", reports=1, amount_rub=3000.0
    )
    inv2 = store.create_payment(7, package_code="report1", reports=1, amount_rub=3000.0)
    assert inv2 == inv1 + 1

    payment = store.get_payment(inv1)
    assert payment["status"] == "pending"
    assert payment["user_id"] == 42

    paid = store.mark_payment_paid(inv1)
    assert paid["reports"] == 1
    assert store.credits(42) == 1
    # повторное зачисление невозможно
    assert store.mark_payment_paid(inv1) is None
    assert store.credits(42) == 1
    store.close()


def test_available_and_consume_order(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3")
    # 3 из 5 бесплатных потрачено
    for _ in range(3):
        store.record(42)
    assert store.available(42, 5) == 2
    # купили один отчёт
    inv = store.create_payment(42, package_code="report1", reports=1, amount_rub=3000.0)
    store.mark_payment_paid(inv)
    assert store.available(42, 5) == 3
    # сначала догорают бесплатные
    assert store.consume(42, 5) == "free"
    assert store.consume(42, 5) == "free"
    assert store.available(42, 5) == 1
    # затем платный кредит
    assert store.consume(42, 5) == "paid"
    assert store.credits(42) == 0
    store.close()


def test_consume_without_credits_falls_back_to_free(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3")
    for _ in range(5):
        store.record(42)
    assert store.consume(42, 5) == "free"  # гонка/безлимит — минуса нет
    assert store.credits(42) == 0
    store.close()
