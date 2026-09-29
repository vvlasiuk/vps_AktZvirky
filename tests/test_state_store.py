from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.state_store import StateStore


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.sqlite3")


def test_full_lifecycle_creating_to_done(tmp_path):
    store = _store(tmp_path)
    store.insert_creating("req-1", "akt_zvirky", "12345", "AKTZVIRKY", {"date_start": "01.09.2026"})

    recoverable = store.load_recoverable()
    assert len(recoverable) == 1
    assert recoverable[0]["status"] == "creating"

    store.mark_polling("req-1", task_id="task-1", storage="storage-1")
    recoverable = store.load_recoverable()
    assert recoverable[0]["status"] == "polling"
    assert recoverable[0]["task_id"] == "task-1"

    store.mark_done("req-1")
    assert store.load_recoverable() == []
    store.close()


def test_error_status_not_recoverable(tmp_path):
    store = _store(tmp_path)
    store.insert_creating("req-err", "akt_zvirky", "1", "AKTZVIRKY", {})
    store.mark_error("req-err", "boom")
    assert store.load_recoverable() == []
    store.close()


def test_recover_after_crash_mid_polling_keeps_task_id(tmp_path):
    """Симулює: процес впав, поки задача чекала відповіді 1С — task_id/storage мають зберегтись."""
    db_path = tmp_path / "state.sqlite3"
    store = StateStore(db_path)
    store.insert_creating("req-2", "akt_zvirky", "999", "AKTZVIRKY", {"date_start": "01.01.2026"})
    store.mark_polling("req-2", task_id="task-2", storage="storage-2")
    store.close()  # імітація падіння процесу без явного done/error

    # новий процес відкриває той самий файл БД
    reopened = StateStore(db_path)
    rows = reopened.load_recoverable()
    assert len(rows) == 1
    assert rows[0]["request_id"] == "req-2"
    assert rows[0]["task_id"] == "task-2"
    assert rows[0]["status"] == "polling"
    reopened.close()


def test_cleanup_old_removes_only_finished_rows(tmp_path):
    store = _store(tmp_path)
    store.insert_creating("req-3", "akt_zvirky", "1", "AKTZVIRKY", {})
    store.mark_done("req-3")
    store.insert_creating("req-4", "akt_zvirky", "1", "AKTZVIRKY", {})  # лишається 'creating'

    # older_than_hours=0 -> все 'done'/'error' одразу застаріле
    removed = store.cleanup_old(older_than_hours=0)
    assert removed == 1
    assert len(store.load_recoverable()) == 1
    store.close()
