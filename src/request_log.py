from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_LOGGER_NAME = "vps_akt_zvirky.requests"


def setup_request_logger(log_file: Path, max_bytes: int, backup_count: int) -> logging.Logger:
    """
    Окремий лог-файл запитів до 1С (типово logs/requests.log) — хто (chat_id/
    username) яку команду запустив і з якими параметрами. Незалежний від
    LOG_LEVEL основного логера (src/logger_setup.py) і не залежить від нього:
    пишеться завжди, по одному рядку (JSON) на кожен запит, що пішов у 1С,
    незалежно від того, чим завершилось виконання.

    Окремий логер (не "vps_akt_zvirky") і окремий файл — свідомо: це аудит-
    лог запитів для людини/розбору, а не діагностика роботи сервісу, тому
    йому не місце впереміш з ERROR/DEBUG-рядками service.log.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_file.parent.mkdir(parents=True, exist_ok=True)

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(message)s")
    handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def log_request(
    logger: logging.Logger,
    *,
    request_id: str,
    chat_id: str,
    username: str | None,
    user_id: str | None,
    command_name: str,
    params: dict[str, Any],
) -> None:
    """Один рядок JSON на запит — легко грепати й парсити пізніше."""
    entry = {
        "request_id": request_id,
        "chat_id": chat_id,
        "username": username,
        "user_id": user_id,
        "command": command_name,
        "params": params,
    }
    try:
        logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        # Лог запитів — допоміжний; збій запису в нього не має валити обробку.
        logger.exception("Не вдалося записати лог запиту")
