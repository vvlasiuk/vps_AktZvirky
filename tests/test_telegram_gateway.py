"""
Перевірка розбору Telegram update'ів — той самий контракт, що вже
реалізований і перевірений у vps_input_msg_telegram/app/telegram_gateway.py,
перенесений сюди. Мережевих викликів тут немає (extract_event — чиста логіка).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.telegram_gateway import TelegramGateway


def _gateway() -> TelegramGateway:
    return TelegramGateway(token="123:FAKE", poll_timeout_sec=30)


def test_extract_plain_text_message():
    update = {
        "update_id": 100500,
        "message": {
            "message_id": 42,
            "date": 1780000000,
            "chat": {"id": 999},
            "from": {"id": 111, "username": "vasyl"},
            "text": "🧾 Акт звірки",
        },
    }
    event = _gateway().extract_event(update)
    assert event is not None
    assert event.event_type == "message"
    assert event.source.chat_id == "999"
    assert event.source.username == "vasyl"
    assert event.text == "🧾 Акт звірки"
    assert event.command_name == ""
    assert event.message_id_int == 42
    assert event.update_id == 100500


def test_extract_message_without_chat_returns_none():
    update = {"update_id": 1, "message": {"message_id": 1, "chat": {}}}
    assert _gateway().extract_event(update) is None


def test_extract_callback_query_uses_data_as_command_name():
    update = {
        "update_id": 5,
        "callback_query": {
            "id": "cbq-1",
            "data": "SOME_ACTION",
            "from": {"id": 222, "username": "olena"},
            "message": {"message_id": 77, "chat": {"id": 888}, "date": 1780000000},
        },
    }
    event = _gateway().extract_event(update)
    assert event is not None
    assert event.event_type == "callback_query"
    assert event.command_name == "SOME_ACTION"
    assert event.callback_query_id == "cbq-1"
    assert event.source.chat_id == "888"
    assert event.message_id_int == 77


def test_extract_callback_query_without_id_returns_none():
    update = {"update_id": 6, "callback_query": {"data": "X", "message": {"chat": {"id": 1}}}}
    assert _gateway().extract_event(update) is None


def test_extract_unknown_update_type_returns_none():
    update = {"update_id": 7, "my_chat_member": {"chat": {"id": 1}}}
    assert _gateway().extract_event(update) is None


def test_extract_web_app_data_real_contract_akt_zvirky_period():
    """
    Точний контракт, який реально надсилає веб-апп
    vps_telegram_webapp_document_period: JSON-рядок з command_name +
    command_params, а не голий {"date_start": ...} на верхньому рівні.
    """
    raw = json.dumps({
        "command_name": "AKTZVIRKY_PERIOD",
        "command_params": {"date_start": "01.03.2026", "date_end": "15.03.2026"},
    })
    update = {
        "update_id": 8,
        "message": {
            "message_id": 9,
            "date": 1780000000,
            "chat": {"id": 555},
            "from": {"id": 1},
            "web_app_data": {"data": raw, "button_text": "🗓️ Звірка за період"},
        },
    }
    event = _gateway().extract_event(update)
    assert event.command_name == "AKTZVIRKY_PERIOD"
    assert event.command_params == {"date_start": "01.03.2026", "date_end": "15.03.2026"}
    # Telegram відхиляє setMessageReaction для повідомлень з web_app_data
    # (MESSAGE_ID_INVALID) — has_web_app_data дозволяє TelegramApp пропустити
    # таку спробу заздалегідь.
    assert event.has_web_app_data is True


def test_extract_plain_message_has_web_app_data_false():
    update = {
        "update_id": 12,
        "message": {"message_id": 1, "chat": {"id": 1}, "from": {"id": 1}, "text": "звичайний текст"},
    }
    event = _gateway().extract_event(update)
    assert event.has_web_app_data is False


def test_extract_web_app_data_accepts_name_and_params_key_aliases():
    raw = json.dumps({"name": "X", "params": {"a": 1}})
    update = {
        "update_id": 9,
        "message": {"message_id": 1, "chat": {"id": 1}, "from": {"id": 1}, "web_app_data": {"data": raw}},
    }
    event = _gateway().extract_event(update)
    assert event.command_name == "X"
    assert event.command_params == {"a": 1}


def test_extract_web_app_data_invalid_json_is_not_a_crash():
    update = {
        "update_id": 10,
        "message": {"message_id": 1, "chat": {"id": 1}, "from": {"id": 1}, "web_app_data": {"data": "not-json"}},
    }
    event = _gateway().extract_event(update)
    assert event.command_name == "web_app_raw"
    assert event.command_params == {"value": "not-json"}


def test_extract_message_without_web_app_data_has_empty_command():
    update = {
        "update_id": 11,
        "message": {"message_id": 1, "chat": {"id": 1}, "from": {"id": 1}, "text": "звичайний текст"},
    }
    event = _gateway().extract_event(update)
    assert event.command_name == ""
    assert event.command_params == {}
