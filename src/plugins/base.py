from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Source:
    system: str
    chat_id: str
    user_id: str | None = None
    username: str | None = None
    message_id: str | None = None


@dataclass
class IncomingEvent:
    """
    Вхідна подія від Telegram: повідомлення, клік inline-кнопки (callback_query)
    або дані веб-аппа. Контракт command_name/command_params узгоджений з тим,
    що вже реалізовано в 1С і в demo_bot-плагіні vps_dispatcher:
    - текстова кнопка reply-клавіатури -> лише `text`, command_name порожній;
    - веб-апп (наприклад, вибір довільного періоду) -> command_name/command_params
      витягуються з JSON {"command_name"|"name"|"command": ..., "command_params"|"params": {...}};
    - callback_query (inline-кнопка) -> command_name = callback_data.
    """

    source: Source
    event_type: str = "message"  # "message" | "callback_query"
    text: str = ""
    command_name: str = ""
    command_params: dict[str, Any] = field(default_factory=dict)
    update_id: int = 0
    message_id_int: int = 0
    callback_query_id: str | None = None
    # True, якщо повідомлення прийшло з web_app_data (Telegram WebApp
    # sendData) — Telegram відхиляє setMessageReaction для таких повідомлень
    # (MESSAGE_ID_INVALID), тому для них реакцію 👀 навіть не намагаємось ставити.
    has_web_app_data: bool = False


@dataclass
class OneCResultEvent:
    """Подія повернення результату з 1С для раніше відправленого запиту цього плагіна."""

    source: Source
    request_id: str
    command_name: str | None
    data: dict[str, Any] | None
    error: str | None
    # Параметри вихідного запиту (напр. date_start/date_end) — щоб плагін міг
    # підписати відповідь періодом і відрізнити її серед кількох одночасних
    # запитів від того самого chat_id.
    request_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PluginOutput:
    kind: str  # "text" | "document"
    chat_id: str
    text: str | None = None
    reply_markup: Any = None
    file_path: str | None = None
    caption: str | None = None


@dataclass
class OneCSubmission:
    """Запит, який плагін хоче віддати на виконання в 1С (через OneCWorker)."""

    command_name: str
    params: dict[str, Any]


@dataclass
class PluginResult:
    outputs: list[PluginOutput] = field(default_factory=list)
    onec_submission: OneCSubmission | None = None


@dataclass
class MatchDecision:
    should_run: bool
    score: float = 0.0
    reason: str = ""


class MessagePlugin(ABC):
    """
    Мінімальний плагіновий контракт, аналогічний до того, що використовується
    у vps_dispatcher (matches()/run()), але без Rabbit/LangGraph/WorkflowState —
    транспорт тут прямий (Telegram <-> OneCWorker в межах одного процесу).

    Щоб додати новий плагін: успадкувати MessagePlugin, реалізувати matches()
    і run(), за потреби on_onec_result(), і зареєструвати екземпляр
    у PluginRegistry (main.py).
    """

    name: str

    @abstractmethod
    def matches(self, event: IncomingEvent) -> MatchDecision: ...

    @abstractmethod
    async def run(self, event: IncomingEvent) -> PluginResult: ...

    async def on_onec_result(self, event: OneCResultEvent) -> PluginResult:
        """Плагіни, що не звертаються до 1С, можуть не перевизначати цей метод."""
        return PluginResult(outputs=[])
