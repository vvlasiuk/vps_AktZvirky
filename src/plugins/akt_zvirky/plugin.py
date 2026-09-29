from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from ..base import (
    IncomingEvent,
    MatchDecision,
    MessagePlugin,
    OneCResultEvent,
    OneCSubmission,
    PluginOutput,
    PluginResult,
)
from ...keyboards import keyboard_from_json

_LOGGER = logging.getLogger("vps_akt_zvirky")

_RESOURCES_DIR = Path(__file__).parent / "resources"
_KEYBOARD_MAIN = _RESOURCES_DIR / "keyboard_main.json"
_KEYBOARD_RECONCILE = _RESOURCES_DIR / "keyboard_reconcile_menu.json"

_BTN_AKT_ZVIRKY = "🧾 Акт звірки"
_BTN_CURRENT_MONTH = "📅 Звірка за поточний місяць"
_BTN_PREVIOUS_MONTH = "📆 Звірка за попередній місяць"
_BTN_YEAR_TO_DATE = "🗓️ Звірка з початку року"
_BTN_BACK = "⬅️ До головного меню"

_MENU_BUTTONS = {
    "/start",
    _BTN_AKT_ZVIRKY,
    _BTN_CURRENT_MONTH,
    _BTN_PREVIOUS_MONTH,
    _BTN_YEAR_TO_DATE,
    _BTN_BACK,
}

_COMMAND_NAME = "AKTZVIRKY"
_COMMAND_COMPLETED = "AKTZVIRKY_COMPLETED"
# Той самий command_name, який реальний веб-апп (vps_telegram_webapp_document_period)
# кладе в web_app_data -> {"command_name": "AKTZVIRKY_PERIOD", "command_params": {...}},
# як і в demo_bot-плагіні vps_dispatcher.
_COMMAND_PERIOD = "AKTZVIRKY_PERIOD"

_FALLBACK_KEYBOARD = {"keyboard": [["/start"]], "resize_keyboard": True}


def _load_keyboard(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "keyboard" not in data:
            raise ValueError(f"Некоректна структура клавіатури: {data!r}")
        return keyboard_from_json(data)
    except Exception:
        _LOGGER.error("Не вдалося прочитати клавіатуру %s", path, exc_info=True)
        return keyboard_from_json(_FALLBACK_KEYBOARD)


class AktZvirkyPlugin(MessagePlugin):
    """
    Єдиний зараз плагін проекту: меню акту звірки -> запит у 1С (AKTZVIRKY) ->
    файл/текст користувачу. Без стану діалогу — кожен клік самодостатній,
    тому окремого WorkflowState (як у vps_dispatcher) тут немає.
    """

    name = "akt_zvirky"

    def __init__(self, temp_files_dir: Path):
        self._temp_files_dir = temp_files_dir

    def matches(self, event: IncomingEvent) -> MatchDecision:
        if event.command_name == _COMMAND_PERIOD:
            return MatchDecision(should_run=True, score=10.0, reason="webapp_period")

        if event.text in _MENU_BUTTONS:
            return MatchDecision(should_run=True, score=10.0, reason="menu_match")

        # Це єдиний плагін у проекті — будь-яке інше повідомлення теж
        # обробляємо (показуємо головне меню), щоб користувач не лишався
        # без відповіді. Коли з'явиться другий плагін, саме тут звузити
        # умову (наприклад, лише коли інші плагіни не підійшли).
        return MatchDecision(should_run=True, score=1.0, reason="fallback_to_menu")

    async def run(self, event: IncomingEvent) -> PluginResult:
        if event.command_name == _COMMAND_PERIOD:
            return self._submit_period(event, event.command_params)

        if event.text in ("/start", _BTN_BACK):
            return self._main_menu(event)

        if event.text == _BTN_AKT_ZVIRKY:
            return self._reconcile_menu(event)

        if event.text == _BTN_CURRENT_MONTH:
            return self._submit_period(event, self._period_bounds("current_month"))

        if event.text == _BTN_PREVIOUS_MONTH:
            return self._submit_period(event, self._period_bounds("previous_month"))

        if event.text == _BTN_YEAR_TO_DATE:
            return self._submit_period(event, self._period_bounds("year_to_date"))

        return self._main_menu(event)

    async def on_onec_result(self, event: OneCResultEvent) -> PluginResult:
        if event.command_name and event.command_name != _COMMAND_COMPLETED:
            _LOGGER.warning("Невідома команда від 1С для akt_zvirky: %s", event.command_name)

        period_label = self._format_period_label(event.request_params)

        if event.error:
            text = f"⚠️ Помилка формування акта звірки{period_label}: {event.error}"
            return PluginResult(
                outputs=[
                    PluginOutput(
                        kind="text",
                        chat_id=event.source.chat_id,
                        text=text,
                    )
                ]
            )

        data = event.data or {}
        # Період — на початку, щоб було видно одразу навіть у списку
        # повідомлень, коли одночасно надіслано кілька різних запитів.
        content_text = f"✅ Акт звірки сформовано{period_label}.\n\n" + (data.get("text") or "")
        content_text = content_text.rstrip()
        file_items = data.get("file") or []

        outputs: list[PluginOutput] = []
        if isinstance(file_items, list):
            for item in file_items:
                if not isinstance(item, dict):
                    continue
                base64_file = item.get("file_base64", "")
                file_name = item.get("file_name", "")
                if not base64_file or not file_name:
                    continue
                try:
                    file_path = self._save_base64_file(base64_file, file_name, event.request_id)
                except Exception:
                    _LOGGER.error("Не вдалося зберегти файл акта звірки", exc_info=True)
                    continue
                outputs.append(
                    PluginOutput(
                        kind="document",
                        chat_id=event.source.chat_id,
                        file_path=str(file_path),
                        caption=content_text,
                    )
                )

        if not outputs:
            outputs.append(PluginOutput(kind="text", chat_id=event.source.chat_id, text=content_text))

        return PluginResult(outputs=outputs)

    def _main_menu(self, event: IncomingEvent) -> PluginResult:
        text = "👋 Привіт! Оберіть дію в меню." if event.text == "/start" else "Головне меню."
        return PluginResult(
            outputs=[
                PluginOutput(
                    kind="text",
                    chat_id=event.source.chat_id,
                    text=text,
                    reply_markup=_load_keyboard(_KEYBOARD_MAIN),
                )
            ]
        )

    def _reconcile_menu(self, event: IncomingEvent) -> PluginResult:
        return PluginResult(
            outputs=[
                PluginOutput(
                    kind="text",
                    chat_id=event.source.chat_id,
                    text="Оберіть період звірки:",
                    reply_markup=_load_keyboard(_KEYBOARD_RECONCILE),
                )
            ]
        )

    @staticmethod
    def _format_period_label(request_params: dict) -> str:
        """
        " за 01.09.2026 — 28.09.2026" — щоб у відповіді 1С було видно, якого
        запиту вона стосується, якщо користувач (або кілька користувачів)
        запустили декілька звірок за різні періоди одночасно.
        """
        date_start = (request_params or {}).get("date_start")
        date_end = (request_params or {}).get("date_end")
        if not date_start or not date_end:
            return ""
        return f" за {date_start} — {date_end}"

    @staticmethod
    def _period_bounds(period_type: str) -> dict[str, str]:
        now = datetime.now()
        if period_type == "current_month":
            date_start = now.replace(day=1)
            date_end = now
        elif period_type == "previous_month":
            first_day_current = now.replace(day=1)
            last_day_previous = first_day_current - timedelta(days=1)
            date_start = last_day_previous.replace(day=1)
            date_end = last_day_previous
        elif period_type == "year_to_date":
            date_start = now.replace(month=1, day=1)
            date_end = now
        else:
            raise ValueError(f"Невідомий тип періоду: {period_type}")
        return {
            "date_start": date_start.strftime("%d.%m.%Y"),
            "date_end": date_end.strftime("%d.%m.%Y"),
        }

    def _submit_period(self, event: IncomingEvent, bounds: dict[str, str]) -> PluginResult:
        date_start = bounds.get("date_start") or bounds.get("start")
        date_end = bounds.get("date_end") or bounds.get("end")

        if not date_start or not date_end:
            return PluginResult(
                outputs=[
                    PluginOutput(
                        kind="text",
                        chat_id=event.source.chat_id,
                        text="Не вдалося визначити період. Спробуйте обрати ще раз.",
                    )
                ]
            )

        outputs = [
            PluginOutput(
                kind="text",
                chat_id=event.source.chat_id,
                text=f"⏳ Формую акт звірки за період {date_start} — {date_end}, зачекайте…",
            )
        ]
        submission = OneCSubmission(
            command_name=_COMMAND_NAME,
            params={
                "chat_id": event.source.chat_id,
                "date_start": date_start,
                "date_end": date_end,
            },
        )
        return PluginResult(outputs=outputs, onec_submission=submission)

    def _save_base64_file(self, base64_str: str, file_name: str, request_id: str) -> Path:
        self._temp_files_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(file_name).name  # захист від path traversal у імені файлу з 1С
        file_path = self._temp_files_dir / f"{request_id}_{safe_name}"
        file_path.write_bytes(base64.b64decode(base64_str))
        return file_path
