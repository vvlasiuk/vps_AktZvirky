"""
Перевірка окремого лог-файлу запитів: хто (chat_id/username) яку команду
запустив і з якими параметрами — незалежно від LOG_LEVEL основного логера.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.request_log import log_request, setup_request_logger


def _fresh_logger(tmp_path: Path, name: str) -> tuple[logging.Logger, Path]:
    # У проді логер одного імені налаштовується один раз (setup_request_logger
    # ідемпотентний — не додає хендлери повторно), тому для ізоляції тестів
    # одне на тест ім'я логера.
    import src.request_log as request_log_module

    request_log_module._LOGGER_NAME = name
    log_file = tmp_path / "requests.log"
    logger = setup_request_logger(log_file, max_bytes=1_048_576, backup_count=2)
    return logger, log_file


def test_log_request_writes_one_json_line(tmp_path):
    logger, log_file = _fresh_logger(tmp_path, "test_requests_1")
    log_request(
        logger,
        request_id="req-1",
        chat_id="777",
        username="vasyl",
        user_id="111",
        command_name="AKTZVIRKY",
        params={"chat_id": "777", "date_start": "01.09.2026", "date_end": "28.09.2026"},
    )
    for handler in logger.handlers:
        handler.flush()

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    # Формат рядка: "<timestamp> <json>" — json-частина починається з першої "{".
    json_part = lines[0][lines[0].index("{"):]
    entry = json.loads(json_part)
    assert entry["chat_id"] == "777"
    assert entry["username"] == "vasyl"
    assert entry["command"] == "AKTZVIRKY"
    assert entry["params"]["date_start"] == "01.09.2026"


def test_log_request_does_not_raise_on_bad_params(tmp_path):
    logger, log_file = _fresh_logger(tmp_path, "test_requests_2")

    class _NotJsonSerializable:
        pass

    # Не має падати навіть якщо params містить щось несеріалізовне.
    log_request(
        logger,
        request_id="req-2",
        chat_id="1",
        username=None,
        user_id=None,
        command_name="X",
        params={"bad": _NotJsonSerializable()},
    )
