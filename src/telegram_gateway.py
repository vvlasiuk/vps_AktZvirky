from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .plugins.base import IncomingEvent, Source

_LOGGER = logging.getLogger("vps_akt_zvirky")


def _log_slow_call(label: str, started: float, threshold_sec: float = 1.0) -> None:
    """
    Діагностика затримок окремого HTTP-виклику до Telegram — рівень DEBUG,
    тож за замовчуванням (LOG_LEVEL=ERROR) нічого не пише ані в консоль,
    ані у файл; вмикається за потреби разовим LOG_LEVEL=DEBUG.
    """
    elapsed = time.monotonic() - started
    if elapsed >= threshold_sec:
        _LOGGER.debug("%s зайняв %.2fс", label, elapsed)


class TelegramGateway:
    """
    Пряма робота з Telegram Bot API через звичайні HTTP-запити — так само,
    як зроблено у vps_input_msg_telegram (telegram_gateway.py) і
    vps_output_msg_telegram (telegram_sender.py), звідки перенесено цю логіку
    майже без змін: getUpdates з offset, setMessageReaction (👀),
    answerCallbackQuery, sendMessage/sendDocument, setMyCommands.

    python-telegram-bot тут більше НЕ використовується для мережі — лише
    для типів клавіатур (src/keyboards.py). Мережевий шар свідомо той самий,
    що вже перевірений у продакшені.
    """

    def __init__(self, token: str, poll_timeout_sec: int):
        self._api_base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self._poll_timeout_sec = max(1, poll_timeout_sec)
        # Один Session на весь час роботи гейтвея — переговорює TCP+TLS до
        # api.telegram.org ОДИН раз і повторно використовує з'єднання
        # (keep-alive) для всіх наступних викликів. Без цього кожен
        # requests.post() міг встановлювати нове з'єднання щоразу, що на
        # деяких мережах додає помітну затримку на кожен запит.
        self._session = requests.Session()

        # Автоматичні швидкі повтори на рівні з'єднання для транзиентних
        # збоїв (502/503/504 від Telegram, обрив з'єднання) — без цього
        # такий збій одразу летить нагору й обробляється лише зовнішнім
        # "спробувати через 2с" у TelegramApp.run_forever(), що повільніше.
        # read=0 навмисно — НЕ ретраїмо read-timeout автоматично: для
        # getUpdates це навмисно довге очікування (long polling), і
        # автоматичний повтор тут тільки заплутав би картину; такі випадки
        # так само йдуть у той самий зовнішній fallback.
        retry = Retry(
            total=2,
            connect=2,
            read=0,
            status=2,
            status_forcelist=[502, 503, 504],
            backoff_factor=0.5,
            allowed_methods=frozenset(["GET", "POST"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=20)
        self._session.mount("https://", adapter)

    # --- вхідний потік -----------------------------------------------------

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        http_timeout = self._poll_timeout_sec + 10
        payload: dict[str, Any] = {"timeout": self._poll_timeout_sec}
        if offset is not None:
            payload["offset"] = offset

        # Не рахуємо як "повільний" сам виклик getUpdates — він навмисно
        # довгий (long polling, до poll_timeout_sec секунд очікування нових
        # апдейтів), це очікувано, а не проблема, тому окремого заміру часу
        # тут не ведемо.
        response = self._session.post(f"{self._api_base}/getUpdates", json=payload, timeout=http_timeout)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API getUpdates failed: {data}")
        return data.get("result", [])

    def extract_event(self, update: dict[str, Any]) -> IncomingEvent | None:
        """
        Повертає IncomingEvent для message/callback_query, або None для будь-якого
        іншого типу update (наприклад, my_chat_member) — такі просто пропускаються.
        """
        update_id = int(update.get("update_id", 0))

        message = update.get("message")
        if isinstance(message, dict):
            return self._extract_from_message(message, update_id)

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            return self._extract_from_callback_query(callback_query, update_id)

        return None

    def _extract_from_message(self, message: dict[str, Any], update_id: int) -> IncomingEvent | None:
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return None

        sender = message.get("from", {})
        command_name, command_params = self._extract_command_from_web_app_data(message.get("web_app_data"))

        return IncomingEvent(
            source=Source(
                system="telegram",
                chat_id=chat_id,
                user_id=str(sender.get("id", "")) or None,
                username=sender.get("username") or None,
                message_id=str(message.get("message_id", "")) or None,
            ),
            event_type="message",
            text=message.get("text") or message.get("caption") or "",
            command_name=command_name or "",
            command_params=command_params,
            update_id=update_id,
            message_id_int=int(message.get("message_id", 0)),
            has_web_app_data=message.get("web_app_data") is not None,
        )

    def _extract_from_callback_query(self, callback_query: dict[str, Any], update_id: int) -> IncomingEvent | None:
        callback_query_id = callback_query.get("id", "")
        if not callback_query_id:
            return None

        message = callback_query.get("message")
        from_user = callback_query.get("from", {})

        if isinstance(message, dict):
            chat = message.get("chat", {})
            chat_id = str(chat.get("id", ""))
            message_id_int = int(message.get("message_id", 0))
        else:
            chat_id = ""
            message_id_int = 0

        if not chat_id:
            return None

        return IncomingEvent(
            source=Source(
                system="telegram",
                chat_id=chat_id,
                user_id=str(from_user.get("id", "")) or None,
                username=from_user.get("username") or None,
                message_id=str(message_id_int) or None,
            ),
            event_type="callback_query",
            text="",
            # Так само, як у vps_input_msg_telegram: callback_data стає command_name.
            command_name=callback_query.get("data", "") or "",
            command_params={},
            update_id=update_id,
            message_id_int=message_id_int,
            callback_query_id=callback_query_id,
        )

    @staticmethod
    def _extract_command_from_web_app_data(web_app_data: Any) -> tuple[str | None, dict[str, Any]]:
        """
        Той самий контракт, що й у vps_input_msg_telegram: веб-апп повертає
        JSON-рядок з полями command_name/name/command і command_params/params.
        Саме так реальний веб-апп 'vps_telegram_webapp_document_period'
        передає AKTZVIRKY_PERIOD з датами.
        """
        if not isinstance(web_app_data, dict):
            return None, {}

        raw_data = web_app_data.get("data")
        if not raw_data or not isinstance(raw_data, str):
            return None, {}

        try:
            parsed = json.loads(raw_data)
        except (TypeError, ValueError):
            return "web_app_raw", {"value": raw_data}

        if not isinstance(parsed, dict):
            return "web_app_raw", {"value": parsed}

        command_name_raw = parsed.get("command_name") or parsed.get("name") or parsed.get("command")
        command_name = str(command_name_raw).strip() if command_name_raw else "web_app"

        params_value = parsed.get("command_params", parsed.get("params", {}))
        if isinstance(params_value, dict):
            command_params = params_value
        elif params_value in (None, ""):
            command_params = {}
        elif isinstance(params_value, str):
            try:
                decoded = json.loads(params_value)
                command_params = decoded if isinstance(decoded, dict) else {"value": decoded}
            except (TypeError, ValueError):
                command_params = {"value": params_value}
        else:
            command_params = {"value": params_value}

        return command_name, command_params

    # --- вихідний потік / пост-обробка --------------------------------------

    def send_message(self, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> int | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        started = time.monotonic()
        response = self._session.post(f"{self._api_base}/sendMessage", json=payload, timeout=30)
        _log_slow_call(f"sendMessage (chat_id={chat_id})", started)
        response.raise_for_status()
        # message_id відправленого повідомлення — потрібен, щоб згодом мати
        # змогу прибрати службові повідомлення (напр. "⏳ Формую...") після
        # того, як прийшов остаточний результат.
        return (response.json().get("result") or {}).get("message_id")

    def send_document(self, chat_id: str, file_path: str, caption: str | None = None) -> int | None:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Файл не знайдено: {path}")
        started = time.monotonic()
        with path.open("rb") as file_obj:
            files = {"document": file_obj}
            data: dict[str, Any] = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            response = self._session.post(f"{self._api_base}/sendDocument", files=files, data=data, timeout=60)
            _log_slow_call(f"sendDocument (chat_id={chat_id})", started)
            response.raise_for_status()
            return (response.json().get("result") or {}).get("message_id")

    def delete_message(self, chat_id: str, message_id: int) -> bool:
        """
        Прибирає службове повідомлення (напр. "⏳ Формую акт звірки...") після
        того, як прийшов остаточний результат. Бот може видаляти вхідні
        повідомлення в приватному чаті й свої власні вихідні — саме цей
        випадок тут і є. Невдача (повідомлення вже видалене, старше 48г
        тощо) — не критична, лише попередження в лог, роботу не зупиняє.
        """
        if not message_id:
            return False
        started = time.monotonic()
        try:
            response = self._session.post(
                f"{self._api_base}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=15,
            )
            _log_slow_call(f"deleteMessage (chat_id={chat_id}, message_id={message_id})", started)
            response.raise_for_status()
            return bool(response.json().get("ok"))
        except requests.exceptions.HTTPError as exc:
            body = exc.response.text if exc.response is not None else ""
            _LOGGER.warning(
                "Не вдалося видалити службове повідомлення %s (chat_id=%s): %s — %s",
                message_id, chat_id, exc, body,
            )
            return False
        except Exception:
            _LOGGER.warning("Не вдалося видалити службове повідомлення %s (chat_id=%s)", message_id, chat_id, exc_info=True)
            return False

    def set_message_reaction_eyes(self, chat_id: str, message_id: int) -> bool:
        if not message_id:
            return False
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": "👀"}],
            "is_big": False,
        }
        started = time.monotonic()
        try:
            response = self._session.post(f"{self._api_base}/setMessageReaction", json=payload, timeout=30)
            _log_slow_call(f"setMessageReaction (chat_id={chat_id})", started)
            response.raise_for_status()
            return bool(response.json().get("ok"))
        except requests.exceptions.HTTPError as exc:
            # Telegram завжди повертає причину в тілі відповіді (поле "description") —
            # без неї 400 неможливо діагностувати, тому логуємо його явно, окремо
            # від голого коду статусу.
            body = exc.response.text if exc.response is not None else ""
            _LOGGER.error(
                "Не вдалося поставити реакцію 👀 (chat_id=%s, message_id=%s): %s — %s",
                chat_id, message_id, exc, body,
            )
            return False
        except Exception:
            _LOGGER.error("Не вдалося поставити реакцію 👀 (chat_id=%s, message_id=%s)", chat_id, message_id, exc_info=True)
            return False

    def answer_callback_query(self, callback_query_id: str) -> bool:
        started = time.monotonic()
        try:
            response = self._session.post(
                f"{self._api_base}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id},
                timeout=10,
            )
            _log_slow_call(f"answerCallbackQuery ({callback_query_id})", started)
            response.raise_for_status()
            return bool(response.json().get("ok"))
        except requests.exceptions.HTTPError as exc:
            body = exc.response.text if exc.response is not None else ""
            _LOGGER.error(
                "Не вдалося підтвердити callback_query %s: %s — %s", callback_query_id, exc, body,
            )
            return False
        except Exception:
            _LOGGER.error("Не вдалося підтвердити callback_query %s", callback_query_id, exc_info=True)
            return False

    def set_bot_commands_from_file(self, commands_path: Path) -> None:
        if not commands_path.exists():
            _LOGGER.info("Файл команд %s не знайдено, команди не встановлюються.", commands_path)
            return
        try:
            commands = json.loads(commands_path.read_text(encoding="utf-8"))
            response = self._session.post(f"{self._api_base}/setMyCommands", json={"commands": commands}, timeout=10)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                _LOGGER.warning("Не вдалося встановити команди бота: %s", data)
            else:
                _LOGGER.info("Команди Telegram-бота встановлено з %s", commands_path)
        except Exception:
            _LOGGER.error("Помилка при встановленні команд Telegram-бота", exc_info=True)
