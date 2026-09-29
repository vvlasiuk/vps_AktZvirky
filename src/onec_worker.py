from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import AppConfig
from .onec_client import OneCClient
from .state_store import StateStore

_LOGGER = logging.getLogger("vps_akt_zvirky")


@dataclass
class OneCRequest:
    request_id: str
    plugin_name: str
    chat_id: str
    command_name: str
    params: dict[str, Any]
    # message_id службових повідомлень (напр. "⏳ Формую...", саме повідомлення
    # користувача, що запустило дію) — прибираються після успішної доставки
    # результату, щоб у чаті не лишався зайвий "шум".
    cleanup_message_ids: list[int] = field(default_factory=list)


@dataclass
class OneCResult:
    request_id: str
    plugin_name: str
    chat_id: str
    command_name: str | None
    data: dict[str, Any] | None
    error: str | None
    # Параметри вихідного запиту (напр. date_start/date_end) — потрібні
    # плагіну в on_onec_result(), щоб підписати відповідь періодом: без
    # цього при кількох одночасних запитах від одного chat_id неможливо
    # зрозуміти, яка відповідь до якого періоду належить.
    request_params: dict[str, Any] = field(default_factory=dict)
    # Прокинуто з OneCRequest.cleanup_message_ids — див. коментар там.
    cleanup_message_ids: list[int] = field(default_factory=list)


@dataclass
class _InFlight:
    request: OneCRequest
    task_id: str
    storage: str
    started_monotonic: float
    next_poll_monotonic: float


class OneCWorker(threading.Thread):
    """
    Окремий потік з ЄДИНОЮ COM-сесією до 1С (COM вимагає STA-апартамент —
    працювати з сесією можна лише з того потоку, де її створено).

    Приймає запити через thread-safe чергу (submit() можна викликати з
    asyncio-циклу без блокування), послідовно створює задачі в 1С
    (CreateTask сама по собі швидка — 1С виконує роботу асинхронно) і
    паралельно опитує всі задачі, що вже виконуються. Результати
    повертаються в asyncio-цикл через run_coroutine_threadsafe.

    Перед CreateTask і після отримання результату стан записується в
    StateStore (SQLite), щоб при рестарті процесу відновити задачі, які
    не встигли завершитись.
    """

    def __init__(
        self,
        cfg: AppConfig,
        logger: logging.Logger,
        state_store: StateStore,
        loop: asyncio.AbstractEventLoop,
        on_result: Callable[[OneCResult], Awaitable[Any]],
    ):
        super().__init__(name="onec-worker", daemon=True)
        self._cfg = cfg
        self._logger = logger
        self._state = state_store
        self._loop = loop
        self._on_result = on_result
        self._queue: "queue.Queue[OneCRequest]" = queue.Queue()
        self._in_flight: list[_InFlight] = []
        self._stop_event = threading.Event()
        self._onec: OneCClient | None = None

    def submit(self, request: OneCRequest) -> None:
        self._state.insert_creating(
            request_id=request.request_id,
            plugin_name=request.plugin_name,
            chat_id=request.chat_id,
            command_name=request.command_name,
            params=request.params,
        )
        self._queue.put_nowait(request)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self._onec = OneCClient(self._cfg)
        self._connect_with_retry()
        self._recover_pending()

        self._logger.info("OneCWorker запущено, з'єднання з 1С встановлено")

        while not self._stop_event.is_set():
            try:
                self._drain_queue()
                self._poll_in_flight()
            except Exception:
                self._logger.error("Неочікувана помилка в циклі OneCWorker", exc_info=True)
            time.sleep(self._cfg.loop_sleep_sec)

        if self._onec is not None:
            self._onec.close()
        self._logger.info("OneCWorker зупинено")

    def _connect_with_retry(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._onec.connect()
                return
            except Exception:
                self._logger.error("Не вдалося підключитись до 1С, повтор через %sс", self._cfg.onec_reconnect_interval_sec, exc_info=True)
                time.sleep(self._cfg.onec_reconnect_interval_sec)

    def _recover_pending(self) -> None:
        for row in self._state.load_recoverable():
            request = OneCRequest(
                request_id=row["request_id"],
                plugin_name=row["plugin_name"],
                chat_id=row["chat_id"],
                command_name=row["command_name"],
                params=json.loads(row["params_json"]),
            )
            if row["status"] == "polling" and row["task_id"]:
                self._in_flight.append(
                    _InFlight(
                        request=request,
                        task_id=row["task_id"],
                        storage=row["storage"] or "",
                        started_monotonic=time.monotonic(),
                        next_poll_monotonic=time.monotonic(),
                    )
                )
                self._logger.info("Відновлено опитування задачі 1С %s після рестарту", row["task_id"])
            else:
                # status == 'creating' без task_id: 1С могла і не отримати
                # задачу до падіння процесу — створюємо заново. Акт звірки —
                # операція читання, повторне формування нешкідливе.
                self._queue.put_nowait(request)
                self._logger.info("Повторно надсилаю запит %s після рестарту", request.request_id)

    def _drain_queue(self) -> None:
        while True:
            try:
                request = self._queue.get_nowait()
            except queue.Empty:
                return
            self._create_task(request)

    def _create_task(self, request: OneCRequest) -> None:
        try:
            task_id, storage = self._onec.create_task(request.command_name, request.params)
        except Exception as exc:
            self._logger.error("Помилка створення задачі в 1С (request_id=%s): %s", request.request_id, exc, exc_info=True)
            self._state.mark_error(request.request_id, str(exc))
            self._deliver(
                OneCResult(
                    request_id=request.request_id,
                    plugin_name=request.plugin_name,
                    chat_id=request.chat_id,
                    command_name=None,
                    data=None,
                    error=str(exc),
                    request_params=request.params,
                    cleanup_message_ids=request.cleanup_message_ids,
                )
            )
            return

        self._state.mark_polling(request.request_id, task_id, storage)
        self._in_flight.append(
            _InFlight(
                request=request,
                task_id=task_id,
                storage=storage,
                started_monotonic=time.monotonic(),
                next_poll_monotonic=time.monotonic(),
            )
        )

    def _poll_in_flight(self) -> None:
        if not self._in_flight:
            return

        now = time.monotonic()
        remaining: list[_InFlight] = []

        for item in self._in_flight:
            if now - item.started_monotonic > self._cfg.task_timeout_sec:
                self._state.mark_error(item.request.request_id, "Таймаут очікування відповіді від 1С")
                self._deliver(
                    OneCResult(
                        request_id=item.request.request_id,
                        plugin_name=item.request.plugin_name,
                        chat_id=item.request.chat_id,
                        command_name=None,
                        data=None,
                        error="Таймаут очікування відповіді від 1С",
                        request_params=item.request.params,
                        cleanup_message_ids=item.request.cleanup_message_ids,
                    )
                )
                continue

            if now < item.next_poll_monotonic:
                remaining.append(item)
                continue

            try:
                is_done, error, command_name, data = self._onec.get_task_state(item.task_id, item.storage)
            except Exception as exc:
                self._logger.error("Помилка опитування задачі %s: %s", item.task_id, exc, exc_info=True)
                self._state.mark_error(item.request.request_id, str(exc))
                self._deliver(
                    OneCResult(
                        request_id=item.request.request_id,
                        plugin_name=item.request.plugin_name,
                        chat_id=item.request.chat_id,
                        command_name=None,
                        data=None,
                        error=str(exc),
                        request_params=item.request.params,
                        cleanup_message_ids=item.request.cleanup_message_ids,
                    )
                )
                continue

            if not is_done:
                item.next_poll_monotonic = now + self._cfg.poll_interval_sec
                remaining.append(item)
                continue

            parsed_data = json.loads(data) if isinstance(data, str) else data
            self._state.mark_done(item.request.request_id)
            self._deliver(
                OneCResult(
                    request_id=item.request.request_id,
                    plugin_name=item.request.plugin_name,
                    chat_id=item.request.chat_id,
                    command_name=command_name,
                    data=parsed_data,
                    error=error,
                    request_params=item.request.params,
                    cleanup_message_ids=item.request.cleanup_message_ids,
                )
            )

        self._in_flight = remaining

    def _deliver(self, result: OneCResult) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(self._on_result(result), self._loop)
        except Exception:
            self._logger.error("Не вдалося передати результат %s у asyncio-цикл", result.request_id, exc_info=True)
            return

        # КРИТИЧНО: run_coroutine_threadsafe() повертає concurrent.futures.Future,
        # і якщо його результат ніхто не перевіряє — будь-який виняток
        # усередині self._on_result(result) (TelegramApp.deliver_onec_result)
        # осідає в цьому Future і зникає БЕЗ жодного логу. Раніше так і було:
        # задача в 1С позначалась 'done', а користувач нічого не отримував,
        # і жодної помилки ніде не з'являлось. Явно перевіряємо .exception()
        # у колбеку і логуємо його, якщо є.
        def _report_delivery_exception(fut: "asyncio.Future") -> None:
            try:
                exc = fut.exception()
            except Exception:
                return
            if exc is not None:
                self._logger.error(
                    "Виняток при доставці результату 1С у Telegram (request_id=%s, chat_id=%s): %s",
                    result.request_id, result.chat_id, exc, exc_info=exc,
                )

        future.add_done_callback(_report_delivery_exception)
