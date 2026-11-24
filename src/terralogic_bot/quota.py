"""Квота на число отчётов: учёт успешных генераций по пользователям.

Обычный пользователь может сгенерировать REPORT_LIMIT отчётов; пользователи
из UNLIMITED_USER_IDS — без ограничений. Списывается только успешно
доставленный PDF (ошибки сбора квоту не тратят). Счётчики хранятся в SQLite
и переживают перезапуск бота. Там же ведётся реестр пользователей
(username, имя, время первого и последнего контакта) для статистики.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("terralogic_bot")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_usage (
    user_id INTEGER PRIMARY KEY,
    reports_used INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    inv_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    package_code TEXT NOT NULL,
    reports INTEGER NOT NULL,
    amount_rub REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    paid_at REAL
);
CREATE TABLE IF NOT EXISTS report_credits (
    user_id INTEGER PRIMARY KEY,
    credits INTEGER NOT NULL DEFAULT 0
)
"""


class QuotaStore:
    """SQLite-счётчик выданных отчётов по user_id."""

    def __init__(self, path: str | Path) -> None:
        self._memory = str(path) == ":memory:"
        if not self._memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def used(self, user_id: int) -> int:
        """Сколько отчётов пользователь уже получил."""
        with self._lock:
            row = self._conn.execute(
                "SELECT reports_used FROM report_usage WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def remaining(self, user_id: int, limit: int) -> int:
        """Сколько отчётов ещё доступно (не меньше нуля)."""
        return max(0, limit - self.used(user_id))

    def credits(self, user_id: int) -> int:
        """Платные отчёты, купленные и ещё не израсходованные."""
        with self._lock:
            row = self._conn.execute(
                "SELECT credits FROM report_credits WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def available(self, user_id: int, limit: int) -> int:
        """Всего доступно: остаток бесплатных + платные кредиты."""
        return self.remaining(user_id, limit) + self.credits(user_id)

    def consume(self, user_id: int, limit: int) -> str:
        """Списать один выданный отчёт: сначала бесплатные, потом кредиты.

        Возвращает источник списания: 'free' | 'paid'.
        """
        if self.used(user_id) < limit:
            self.record(user_id)
            return "free"
        with self._lock:
            row = self._conn.execute(
                "SELECT credits FROM report_credits WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            current = int(row[0]) if row else 0
            if current <= 0:
                # гонка с параллельным запросом — считаем в бесплатный счётчик,
                # чтобы не уйти в минус по кредитам
                self._conn.execute(
                    "INSERT INTO report_usage (user_id, reports_used, updated_at) "
                    "VALUES (?, 1, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "reports_used = reports_used + 1, "
                    "updated_at = excluded.updated_at",
                    (user_id, time.time()),
                )
                self._conn.commit()
                return "free"
            self._conn.execute(
                "UPDATE report_credits SET credits = credits - 1 WHERE user_id = ?",
                (user_id,),
            )
            self._conn.commit()
        return "paid"

    def create_payment(
        self,
        user_id: int,
        *,
        package_code: str,
        reports: int,
        amount_rub: float,
    ) -> int:
        """Зарегистрировать новый счёт; вернуть его inv_id (монотонный)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(inv_id), 0) FROM payments"
            ).fetchone()
            inv_id = int(row[0]) + 1
            self._conn.execute(
                "INSERT INTO payments "
                "(inv_id, user_id, package_code, reports, amount_rub, "
                " status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (inv_id, user_id, package_code, reports, amount_rub, time.time()),
            )
            self._conn.commit()
        return inv_id

    def get_payment(self, inv_id: int) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT inv_id, user_id, package_code, reports, amount_rub, "
                "status, created_at, paid_at FROM payments WHERE inv_id = ?",
                (inv_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "inv_id": row[0],
            "user_id": row[1],
            "package_code": row[2],
            "reports": row[3],
            "amount_rub": row[4],
            "status": row[5],
            "created_at": row[6],
            "paid_at": row[7],
        }

    def mark_payment_paid(self, inv_id: int) -> dict[str, object] | None:
        """Перевести счёт в 'paid' и начислить кредиты.

        Идемпотентно: повторный вызов по уже оплаченному счёту ничего не
        начисляет и возвращает None.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE payments SET status = 'paid', paid_at = ? "
                "WHERE inv_id = ? AND status = 'pending'",
                (time.time(), inv_id),
            )
            if cursor.rowcount == 0:
                self._conn.commit()
                return None
            row = self._conn.execute(
                "SELECT user_id, reports FROM payments WHERE inv_id = ?",
                (inv_id,),
            ).fetchone()
            user_id, reports = int(row[0]), int(row[1])
            self._conn.execute(
                "INSERT INTO report_credits (user_id, credits) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "credits = credits + excluded.credits",
                (user_id, reports),
            )
            self._conn.commit()
        payment = self.get_payment(inv_id)
        assert payment is not None
        return payment

    def record(self, user_id: int) -> int:
        """Учесть выданный отчёт; вернуть новое значение счётчика."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO report_usage (user_id, reports_used, updated_at) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "reports_used = reports_used + 1, updated_at = excluded.updated_at",
                (user_id, time.time()),
            )
            self._conn.commit()
        return self.used(user_id)

    def touch_user(
        self,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        """Запомнить/обновить профиль пользователя по данным Telegram.

        first_seen_at ставится при первом контакте и дальше не меняется;
        профильные поля отражают последнее известное состояние.
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users "
                "(user_id, username, first_name, last_name, "
                " first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "username = excluded.username, "
                "first_name = excluded.first_name, "
                "last_name = excluded.last_name, "
                "last_seen_at = excluded.last_seen_at",
                (user_id, username, first_name, last_name, now, now),
            )
            self._conn.commit()

    def list_users(self) -> list[dict[str, object]]:
        """Все известные пользователи, свежие сверху."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, username, first_name, last_name, "
                "first_seen_at, last_seen_at FROM users "
                "ORDER BY last_seen_at DESC"
            ).fetchall()
        return [
            {
                "user_id": row[0],
                "username": row[1],
                "first_name": row[2],
                "last_name": row[3],
                "first_seen_at": row[4],
                "last_seen_at": row[5],
            }
            for row in rows
        ]


def remember_user(quota: QuotaStore | None, from_user: Any) -> None:
    """Записать профиль пользователя в реестр; сбой БД не роняет handler."""
    if quota is None or from_user is None:
        return
    try:
        quota.touch_user(
            from_user.id,
            username=getattr(from_user, "username", None),
            first_name=getattr(from_user, "first_name", None),
            last_name=getattr(from_user, "last_name", None),
        )
    except sqlite3.Error:
        LOGGER.debug("user registry update failed: user=%s", from_user.id)
