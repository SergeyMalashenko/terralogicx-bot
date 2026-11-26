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
    assert settings.report_limit == 0  # бесплатных отчётов нет
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


def test_has_paid_payment(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3")
    assert store.has_paid_payment(42) is False
    inv_id = store.create_payment(
        42, package_code="report1", reports=1, amount_rub=500.0
    )
    assert store.has_paid_payment(42) is False  # ещё pending
    store.mark_payment_paid(inv_id)
    assert store.has_paid_payment(42) is True
    assert store.has_paid_payment(43) is False  # другой пользователь
    store.close()


# ---------------------------------------------------------------------------
# Заказ: предмет, версия оферты, редакции оферты
# ---------------------------------------------------------------------------


def test_payment_stores_subject_and_offer_version(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3")
    inv_id = store.create_payment(
        42,
        package_code="report1",
        reports=1,
        amount_rub=3000.0,
        subject_kind="cadastral",
        subject="50:21:0030210:123",
        offer_version="2026-11-26",
    )
    payment = store.get_payment(inv_id)
    assert payment["subject_kind"] == "cadastral"
    assert payment["subject"] == "50:21:0030210:123"
    assert payment["offer_version"] == "2026-11-26"
    store.close()


def test_payment_without_subject_keeps_nullable_fields(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3")
    inv_id = store.create_payment(
        42, package_code="report1", reports=1, amount_rub=3000.0
    )
    payment = store.get_payment(inv_id)
    assert payment["subject_kind"] is None
    assert payment["subject"] is None
    assert payment["offer_version"] is None
    store.close()


def test_payments_migrated_from_old_schema(tmp_path):
    """БД старой схемы (без предмета заказа) докатывается без потери строк."""
    import sqlite3

    path = tmp_path / "quota.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE payments ("
        "inv_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
        "package_code TEXT NOT NULL, reports INTEGER NOT NULL, "
        "amount_rub REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
        "created_at REAL NOT NULL, paid_at REAL)"
    )
    conn.execute(
        "INSERT INTO payments (inv_id, user_id, package_code, reports, "
        "amount_rub, status, created_at) VALUES (1, 42, 'report1', 1, "
        "3000.0, 'paid', 1.0)"
    )
    conn.commit()
    conn.close()

    store = QuotaStore(path)  # открытие выполняет миграцию
    payment = store.get_payment(1)
    assert payment["user_id"] == 42
    assert payment["status"] == "paid"
    assert payment["subject"] is None
    # новые счета уже с предметом
    inv_id = store.create_payment(
        7,
        package_code="report1",
        reports=1,
        amount_rub=500.0,
        subject_kind="cadastral",
        subject="50:21:0030210:123",
        offer_version="2026-11-26",
    )
    assert store.get_payment(inv_id)["subject"] == "50:21:0030210:123"
    store.close()


def test_offer_versions_registered_once_and_readable(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3")
    store.register_offer_version("2026-11-26", "текст редакции v1")
    store.register_offer_version("2026-11-26", "подменённый текст")
    store.register_offer_version("2026-12-01", "текст редакции v2")

    # первая редакция не перезаписывается: условия старых заказов сохраняются
    assert store.offer_text("2026-11-26") == "текст редакции v1"
    assert store.offer_text("2026-12-01") == "текст редакции v2"
    assert store.offer_text("2099-01-01") is None
    assert store.list_offer_versions() == ["2026-11-26", "2026-12-01"]
    store.close()
