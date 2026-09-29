from __future__ import annotations

from typing import Any

from telegram import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo


def keyboard_from_json(data: dict[str, Any]) -> ReplyKeyboardMarkup:
    """
    Перетворює клавіатуру, описану у форматі resources/*.json (той самий
    формат, що й keyboard.json у vps_input_msg_telegram / vps_dispatcher),
    у об'єкт python-telegram-bot. Рядок клавіатури може містити або просто
    текст кнопки, або словник з web_app-кнопкою.
    """
    rows: list[list[Any]] = []
    for row in data.get("keyboard", []):
        new_row: list[Any] = []
        for item in row:
            if isinstance(item, str):
                new_row.append(item)
            elif isinstance(item, dict):
                web_app = item.get("web_app")
                new_row.append(
                    KeyboardButton(
                        text=item.get("text", ""),
                        web_app=WebAppInfo(url=web_app["url"]) if web_app else None,
                    )
                )
        rows.append(new_row)

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=bool(data.get("resize_keyboard", True)),
        one_time_keyboard=bool(data.get("one_time_keyboard", False)),
        is_persistent=bool(data.get("is_persistent", False)),
    )
