"""
Перевірка OneCWorker під навантаженням, БЕЗ реального 1С: OneCClient
підмінюється фейком, що імітує CreateTask (миттєво) + StatusTask
(готово після 2-го опитування). Перевіряємо, що черга з багатьох
одночасних запитів (як від багатьох користувачів Telegram) коректно
серіалізується в один потік, кожен запит доходить до правильного
chat_id, і стан у StateStore узгоджений після завершення.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Заглушки pywin32 (Windows-only) — щоб onec_client.py імпортувався на будь-якій ОС у тестах.
sys.modules.setdefault("pythoncom", types.ModuleType("pythoncom"))
_win32com = types.ModuleType("win32com")
_win32com_client = types.ModuleType("win32com.client")
_win32com.client = _win32com_client
sys.modules.setdefault("win32com", _win32com)
sys.modules.setdefault("win32com.client", _win32com_client)

import src.onec_worker as onec_worker_module  # noqa: E402
from src.onec_worker import OneCRequest, OneCWorker  # noqa: E402
from src.state_store import StateStore  # noqa: E402


class _FakeOneCClient:
    """Імітує 1С: CreateTask повертає одразу, StatusTask 'готовий' з 2-ї спроби."""

    def __init__(self, cfg):
        self._cfg = cfg
        self._polls: dict[str, int] = {}
        self.create_calls = 0

    def connect(self):
        pass

    def close(self):
        pass

    def create_task(self, task_name: str, params: dict):
        self.create_calls += 1
        task_id = f"task-{uuid4().hex[:8]}"
        self._polls[task_id] = 0
        # chat_id несемо в task_id, щоб фейкова 1С "знала", кому яка відповідь
        return task_id, params["chat_id"]

    def get_task_state(self, task_id: str, storage: str):
        self._polls[task_id] = self._polls.get(task_id, 0) + 1
        if self._polls[task_id] < 2:
            return False, None, None, None
        data = f'{{"text": "Готово для {storage}"}}'
        return True, None, "AKTZVIRKY_COMPLETED", data


def test_worker_handles_many_concurrent_users_correctly(tmp_path, monkeypatch):
    monkeypatch.setattr(onec_worker_module, "OneCClient", _FakeOneCClient)

    class _FakeConfig:
        loop_sleep_sec = 0.01
        poll_interval_sec = 0.01
        task_timeout_sec = 5.0
        onec_reconnect_interval_sec = 0.1

    cfg = _FakeConfig()
    state_store = StateStore(tmp_path / "state.sqlite3")

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    delivered: list = []
    delivered_event = threading.Event()
    expected_count = 40

    async def on_result(result):
        delivered.append(result)
        if len(delivered) == expected_count:
            delivered_event.set()

    import logging

    worker = OneCWorker(cfg, logging.getLogger("test"), state_store, loop, on_result)
    worker.start()

    # Багато "користувачів" одночасно надсилають запити (submit() з різних потоків,
    # так само як це робив би asyncio-обробник Telegram для кожного клієнта).
    def submit_one(i: int):
        worker.submit(
            OneCRequest(
                request_id=uuid4().hex,
                plugin_name="akt_zvirky",
                chat_id=str(1000 + i),
                command_name="AKTZVIRKY",
                params={"chat_id": str(1000 + i), "date_start": "01.09.2026", "date_end": "28.09.2026"},
            )
        )

    submitters = [threading.Thread(target=submit_one, args=(i,)) for i in range(expected_count)]
    for t in submitters:
        t.start()
    for t in submitters:
        t.join()

    assert delivered_event.wait(timeout=10), f"Отримано лише {len(delivered)} з {expected_count} результатів"

    worker.stop()
    worker.join(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=5)

    # Кожен користувач отримав СВОЮ відповідь, жодна не переплуталась і не загубилась
    by_chat_id = {r.chat_id: r for r in delivered}
    assert len(by_chat_id) == expected_count
    for i in range(expected_count):
        chat_id = str(1000 + i)
        assert chat_id in by_chat_id
        assert by_chat_id[chat_id].error is None
        assert f"Готово для {chat_id}" in by_chat_id[chat_id].data["text"]

    # У сховищі стану всі задачі позначені виконаними
    assert state_store.load_recoverable() == []
    state_store.close()


def test_worker_recovers_in_flight_task_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(onec_worker_module, "OneCClient", _FakeOneCClient)

    class _FakeConfig:
        loop_sleep_sec = 0.01
        poll_interval_sec = 0.01
        task_timeout_sec = 5.0
        onec_reconnect_interval_sec = 0.1

    cfg = _FakeConfig()
    db_path = tmp_path / "state.sqlite3"

    # Крок 1: імітуємо, що задача вже "в польоті" (перед падінням процесу)
    state_store = StateStore(db_path)
    state_store.insert_creating("req-crash", "akt_zvirky", "555", "AKTZVIRKY", {"chat_id": "555"})
    state_store.mark_polling("req-crash", task_id="task-crash", storage="555")
    state_store.close()

    # Крок 2: "новий процес" піднімає воркер на тій самій БД — задача має відновитись
    state_store2 = StateStore(db_path)
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    delivered = []
    delivered_event = threading.Event()

    async def on_result(result):
        delivered.append(result)
        delivered_event.set()

    import logging

    worker = OneCWorker(cfg, logging.getLogger("test"), state_store2, loop, on_result)
    worker.start()

    assert delivered_event.wait(timeout=10)
    worker.stop()
    worker.join(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=5)

    assert delivered[0].chat_id == "555"
    assert delivered[0].error is None
    state_store2.close()
