from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateStore:
    """
    Легка персистентність лише для задач, що очікують відповіді від 1С
    (вікно ризику: процес впав між CreateTask і доставкою результату
    користувачу). Жодного діалогового стану тут немає — кожен клік
    у боті самодостатній, тож повноцінна БД для WorkflowState не потрібна.

    SQLite обраний свідомо: не потребує окремого сервера (важливо для
    швидкого розгортання у клієнта), при цьому WAL-режим дозволяє
    безпечно читати з одного потоку, поки інший пише.
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_tasks (
                    request_id TEXT PRIMARY KEY,
                    plugin_name TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    command_name TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    task_id TEXT,
                    storage TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    @staticmethod
    def _now() -> str:
        # Формат сумісний з sqlite datetime()/'now' для порівнянь у cleanup_old.
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def insert_creating(
        self,
        request_id: str,
        plugin_name: str,
        chat_id: str,
        command_name: str,
        params: dict[str, Any],
    ) -> None:
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO pending_tasks
                    (request_id, plugin_name, chat_id, command_name, params_json,
                     task_id, storage, status, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, 'creating', NULL, ?, ?)
                """,
                (request_id, plugin_name, chat_id, command_name, json.dumps(params, ensure_ascii=False), now, now),
            )
            self._conn.commit()

    def mark_polling(self, request_id: str, task_id: str, storage: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_tasks SET status='polling', task_id=?, storage=?, updated_at=? WHERE request_id=?",
                (task_id, storage, self._now(), request_id),
            )
            self._conn.commit()

    def mark_done(self, request_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_tasks SET status='done', updated_at=? WHERE request_id=?",
                (self._now(), request_id),
            )
            self._conn.commit()

    def mark_error(self, request_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_tasks SET status='error', error=?, updated_at=? WHERE request_id=?",
                (error, self._now(), request_id),
            )
            self._conn.commit()

    def load_recoverable(self) -> list[sqlite3.Row]:
        """Задачі, які не встигли завершитись до попереднього рестарту/падіння."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM pending_tasks WHERE status IN ('creating', 'polling') ORDER BY created_at"
            )
            return cursor.fetchall()

    def cleanup_old(self, older_than_hours: int = 72) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                DELETE FROM pending_tasks
                WHERE status IN ('done', 'error')
                  AND updated_at <= datetime('now', ?)
                """,
                (f"-{older_than_hours} hours",),
            )
            self._conn.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
