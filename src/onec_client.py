from __future__ import annotations

import json
import logging
import time
from typing import Any

import pythoncom
import win32com.client

from .config import AppConfig

_LOGGER = logging.getLogger("vps_akt_zvirky")


class OneCClient:
    """
    COM-міст до 1С. Портовано майже без змін з vps_output_msg_1C —
    там ця логіка вже перевірена в продакшені (створення задачі,
    опитування статусу, реконект при втраті сесії).
    """

    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._session = None
        self._inited = False

    def connect(self) -> None:
        pythoncom.CoInitialize()
        self._inited = True

        if self._cfg.onec_file_path:
            connection_string = f'File="{self._cfg.onec_file_path}";Usr={self._cfg.onec_user};Pwd={self._cfg.onec_password};'
        else:
            connection_string = (
                f'Srvr={self._cfg.onec_server};Ref={self._cfg.onec_ref};'
                f'Usr={self._cfg.onec_user};Pwd={self._cfg.onec_password};'
            )

        self._session = win32com.client.Dispatch(self._cfg.onec_connector_prog_id).Connect(connection_string)

    def close(self) -> None:
        self._session = None
        if self._inited:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._inited = False

    def is_alive(self, reconnect: bool = True) -> bool:
        try:
            if self._session is None:
                raise RuntimeError("1C session is not connected")
            result = self._session.VPS.LifeIs()
            if isinstance(result, str) and result.strip().lower() == "true":
                return True
            last_error = f"LifeIs returned: {result}"
        except Exception as e:
            last_error = str(e)

        if reconnect:
            try:
                self.connect()
                result = self._session.VPS.LifeIs()
                if isinstance(result, str) and result.strip().lower() == "true":
                    return True
                last_error = f"LifeIs after reconnect: {result}"
            except Exception as e:
                last_error = f"Reconnect error: {e}"

        _LOGGER.error("1C session lost: %s", last_error)
        time.sleep(self._cfg.onec_reconnect_interval_sec)
        return False

    def _new_structure(self):
        if not self.is_alive():
            raise RuntimeError("1C session is not active")
        return self._session.NewObject("Structure")

    @staticmethod
    def _insert_to_structure(structure: Any, key: str, value: Any) -> None:
        try:
            structure.Insert(key, value)
        except Exception:
            structure.Вставить(key, value)

    def create_task(self, task_name: str, params: dict[str, Any]) -> tuple[str, str]:
        if not self.is_alive():
            raise RuntimeError("1C session is not active")

        structure = self._new_structure()
        for key, value in params.items():
            self._insert_to_structure(structure, key, value)

        task = self._session.VPS.CreateTask(task_name, structure)

        if not isinstance(task, str):
            raise RuntimeError("Очікується відповідь у форматі JSON (str)")
        task = json.loads(task)

        raw_status = task.get("status")
        raw_error = task.get("text_error")
        task_id = task.get("TaskID")
        storage = task.get("Storage")

        if str(raw_status).strip().upper() != "OK":
            raise RuntimeError(str(raw_error or "1C CreateTask error"))

        return task_id, storage

    def get_task_state(
        self,
        task_id: str,
        storage: str,
    ) -> tuple[bool, str | None, str | None, Any | None]:
        if not self.is_alive():
            raise RuntimeError("1C session is not active")

        status_result = self._session.VPS.StatusTask(task_id, storage)

        if isinstance(status_result, str):
            text = status_result.strip()
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None

            if isinstance(parsed, dict):
                status_value = str(parsed.get("status", "")).upper()
                error_value = parsed.get("text_errror")
                command_value = parsed.get("command")

                if status_value == "OK":
                    data_value = parsed.get("DATA", parsed.get("data"))
                    return True, None, command_value, data_value
                if status_value == "ERROR":
                    return True, str(error_value or "1C task error"), command_value, None
                if status_value == "RUN":
                    return False, None, None, None

        if isinstance(status_result, bool):
            return (True, None, None, None) if status_result else (False, None, None, None)

        raw_status = str(status_result)
        normalized = raw_status.upper()

        if normalized in {"TRUE", "1", "OK", "DONE", "FINISHED", "SUCCESS"}:
            return True, None, None, None
        if normalized in {"FALSE", "0", "PENDING", "INPROGRESS", "IN_PROGRESS", "RUNNING"}:
            return False, None, None, None
        if "ОШИБ" in normalized or "ERROR" in normalized or "FAIL" in normalized:
            return True, raw_status or "1C task error", None, None

        for attr in ("Status", "Статус", "State", "Состояние"):
            try:
                attr_value = str(getattr(status_result, attr))
            except Exception:
                continue

            state = attr_value.upper()
            if "ОШИБ" in state or "ERROR" in state or "FAIL" in state:
                return True, attr_value or "1C task error", None, None
            if "OK" in state or "DONE" in state or "ЗАВЕРШ" in state:
                return True, None, None, None

        return False, None, None, None
