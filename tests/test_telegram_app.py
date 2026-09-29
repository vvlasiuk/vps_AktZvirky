"""
Перевірка TelegramApp без мережі: TelegramGateway підмінюється фейком.
Головне, що тут перевіряється (реальні прогалини, знайдені при звірці з
vps_input_msg_telegram/vps_output_msg_telegram):
- offset просувається лише до першого невдалого update'у в пачці;
- реакція 👀 ставиться на message-подіях ПІСЛЯ успішної обробки;
- callback_query підтверджується (answerCallbackQuery) після обробки;
- жодної пост-обробки не відбувається, якщо плагін впав з винятком.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.plugins.base import IncomingEvent, MatchDecision, MessagePlugin, OneCSubmission, PluginOutput, PluginResult, Source
from src.plugins.registry import PluginRegistry
from src.telegram_app import TelegramApp


class _EchoPlugin(MessagePlugin):
    """Плагін-заглушка: відповідає текстом, і падає, якщо chat_id == 'boom'."""

    name = "echo"

    def matches(self, event: IncomingEvent) -> MatchDecision:
        return MatchDecision(should_run=True, score=1.0)

    async def run(self, event: IncomingEvent) -> PluginResult:
        if event.source.chat_id == "boom":
            raise RuntimeError("навмисний збій для тесту")
        submission = OneCSubmission(command_name="X", params={"chat_id": event.source.chat_id}) if event.text == "submit" else None
        return PluginResult(
            outputs=[PluginOutput(kind="text", chat_id=event.source.chat_id, text=f"echo:{event.text}")],
            onec_submission=submission,
        )


class _FakeGateway:
    def __init__(self):
        self.sent_messages: list[tuple[str, str]] = []
        self.reactions: list[tuple[str, int]] = []
        self.answered_callbacks: list[str] = []
        self.deleted_messages: list[tuple[str, int]] = []
        self._next_message_id = 1000

    def extract_event(self, update: dict):
        # у тестах "update" — це вже готовий IncomingEvent, підкладений напряму
        return update.get("__event__")

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent_messages.append((chat_id, text))
        self._next_message_id += 1
        return self._next_message_id

    def send_document(self, chat_id, file_path, caption=None):
        self._next_message_id += 1
        return self._next_message_id

    def set_message_reaction_eyes(self, chat_id, message_id):
        self.reactions.append((chat_id, message_id))
        return True

    def answer_callback_query(self, callback_query_id):
        self.answered_callbacks.append(callback_query_id)
        return True

    def delete_message(self, chat_id, message_id):
        self.deleted_messages.append((chat_id, message_id))
        return True


def _make_app(tmp_path) -> tuple[TelegramApp, _FakeGateway]:
    class _FakeConfig:
        telegram_token = "123:FAKE"
        telegram_poll_timeout = 30
        offset_file = tmp_path / "offset.txt"
        commands_file = tmp_path / "commands.json"

    registry = PluginRegistry([_EchoPlugin()])
    app = TelegramApp(_FakeConfig(), registry)
    fake_gateway = _FakeGateway()
    app._gateway = fake_gateway  # підміна мережевого шару на фейк
    return app, fake_gateway


def _event(
    chat_id: str,
    text: str = "",
    event_type: str = "message",
    message_id_int: int = 1,
    callback_query_id=None,
    has_web_app_data: bool = False,
) -> IncomingEvent:
    return IncomingEvent(
        source=Source(system="telegram", chat_id=chat_id),
        event_type=event_type,
        text=text,
        message_id_int=message_id_int,
        callback_query_id=callback_query_id,
        has_web_app_data=has_web_app_data,
    )


def test_message_gets_eyes_reaction_after_successful_handling(tmp_path):
    app, gateway = _make_app(tmp_path)
    event = _event("1", text="hello", message_id_int=42)
    ok = asyncio.run(app._handle_update({"__event__": event}))
    assert ok is True
    assert gateway.reactions == [("1", 42)]
    assert gateway.sent_messages == [("1", "echo:hello")]


def test_web_app_data_message_skips_reaction_attempt(tmp_path):
    """
    Telegram відхиляє setMessageReaction для повідомлень з web_app_data
    (MESSAGE_ID_INVALID) — навіть не намагаємось, щоб не засмічувати лог
    гарантовано невдалими викликами.
    """
    app, gateway = _make_app(tmp_path)
    event = _event("1", text="", message_id_int=42, has_web_app_data=True)
    ok = asyncio.run(app._handle_update({"__event__": event}))
    assert ok is True
    assert gateway.reactions == []


def test_callback_query_gets_answered_not_reacted(tmp_path):
    app, gateway = _make_app(tmp_path)
    event = _event("1", event_type="callback_query", callback_query_id="cbq-9", message_id_int=5)
    ok = asyncio.run(app._handle_update({"__event__": event}))
    assert ok is True
    assert gateway.answered_callbacks == ["cbq-9"]
    assert gateway.reactions == []  # реакція лише на message, не на callback_query


def test_failed_plugin_run_skips_reaction_and_returns_false(tmp_path):
    app, gateway = _make_app(tmp_path)
    event = _event("boom", text="hello")
    ok = asyncio.run(app._handle_update({"__event__": event}))
    assert ok is False
    assert gateway.reactions == []  # жодної реакції на провалений update


def test_onec_submission_is_forwarded_to_worker(tmp_path):
    app, gateway = _make_app(tmp_path)

    submitted = []

    class _FakeWorker:
        def submit(self, request):
            submitted.append(request)

    app.attach_worker(_FakeWorker())
    event = _event("42", text="submit", message_id_int=7)
    asyncio.run(app._handle_update({"__event__": event}))

    assert len(submitted) == 1
    assert submitted[0].chat_id == "42"
    assert submitted[0].command_name == "X"
    # Повідомлення, що запустило дію (message_id_int=7), і "⏳ Формую..."
    # (message_id, повернений фейковим send_message) — кандидати на видалення
    # після успішної доставки результату.
    assert 7 in submitted[0].cleanup_message_ids
    assert len(submitted[0].cleanup_message_ids) == 2


def test_cleanup_messages_deleted_after_successful_delivery(tmp_path):
    from src.onec_worker import OneCResult

    app, gateway = _make_app(tmp_path)
    result = OneCResult(
        request_id="r1",
        plugin_name="echo",
        chat_id="42",
        command_name=None,
        data=None,
        error=None,
        cleanup_message_ids=[7, 1001],
    )
    asyncio.run(app.deliver_onec_result(result))
    assert set(gateway.deleted_messages) == {("42", 7), ("42", 1001)}


def test_cleanup_messages_kept_after_error(tmp_path):
    from src.onec_worker import OneCResult

    app, gateway = _make_app(tmp_path)
    result = OneCResult(
        request_id="r2",
        plugin_name="echo",
        chat_id="42",
        command_name=None,
        data=None,
        error="1С недоступна",
        cleanup_message_ids=[7, 1001],
    )
    asyncio.run(app.deliver_onec_result(result))
    assert gateway.deleted_messages == []


def test_offset_advances_only_up_to_first_failure_in_batch():
    # 3 успішні, потім невдалий (Exception), потім ще один успішний, якого
    # НЕ мало бути враховано у новому offset (він піде на повторну обробку).
    results = [True, True, RuntimeError("boom"), True]
    update_ids = [10, 11, 12, 13]
    new_offset = TelegramApp._compute_new_offset(current_offset=10, update_ids=update_ids, results=results)
    assert new_offset == 12  # просунулись рівно до (не включно) update_id=12, що впав


def test_offset_does_not_move_when_first_update_fails():
    results = [False, True, True]
    update_ids = [50, 51, 52]
    new_offset = TelegramApp._compute_new_offset(current_offset=50, update_ids=update_ids, results=results)
    assert new_offset == 50  # жодного успіху перед провалом — offset лишається на місці


def test_offset_advances_past_whole_batch_when_all_succeed():
    results = [True, True, True]
    update_ids = [1, 2, 3]
    new_offset = TelegramApp._compute_new_offset(current_offset=1, update_ids=update_ids, results=results)
    assert new_offset == 4
