"""Квота на число отчётов: учёт успешных генераций по пользователям.

Обычный пользователь может сгенерировать REPORT_LIMIT отчётов; пользователи
из UNLIMITED_USER_IDS — без ограничений. Списывается только успешно
доставленный PDF (ошибки сбора квоту не тратят). Счётчики хранятся в SQLite
и переживают перезапуск бота.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_usage (
    user_id INTEGER PRIMARY KEY,
    reports_used INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
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
            self._conn.execute(_SCHEMA)
            self._conn.commit()

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
