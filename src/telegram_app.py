from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from .config import AppConfig
from .offset_store import OffsetStore
from .onec_worker import OneCRequest, OneCResult, OneCWorker
from .plugins.base import OneCResultEvent, PluginOutput, Source
from .plugins.registry import PluginRegistry
from .request_log import log_request
from .telegram_gateway import TelegramGateway

_LOGGER = logging.getLogger("vps_akt_zvirky")
_REQUEST_LOGGER = logging.getLogger("vps_akt_zvirky.requests")


class TelegramApp:
    """
    Прийом (long polling) і відправка повідомлень Telegram в межах одного
    процесу — заміняє собою пару vps_input_msg_telegram / vps_output_msg_telegram.

    Цикл опитування навмисно НЕ використовує python-telegram-bot Application/
    Updater, а повторює модель, вже перевірену в проді
    (vps_input_msg_telegram/app/service.py): offset для getUpdates
    зберігається на диск і просувається ЛИШЕ після успішної обробки update'у.
    Якщо обробка впаде — offset лишається на місці, і Telegram поверне те саме
    повідомлення знову після рестарту (безкоштовна гарантія доставки без БД).

    Відмінність від оригіналу — заради навантаження ("багато користувачів
    одночасно"): усі update'и, отримані в одному виклику getUpdates,
    обробляються ПАРАЛЕЛЬНО (asyncio.gather), а не по одному послідовно.
    offset просувається аж до (не включно) першого update'у в пачці, що впав —
    тобто ті, що встигли обробитись паралельно ПІСЛЯ невдалого, можуть при
    падінні процесу обробитись вдруге (для меню/акту звірки це нешкідливо —
    повторний клік просто ще раз сформує звірку).
    """

    def __init__(self, cfg: AppConfig, registry: PluginRegistry):
        self._cfg = cfg
        self._registry = registry
        self._gateway = TelegramGateway(cfg.telegram_token, cfg.telegram_poll_timeout)
        self._offset_store = OffsetStore(cfg.offset_file)
        self._worker: OneCWorker | None = None
        self._stop_event = asyncio.Event()

    def attach_worker(self, worker: OneCWorker) -> None:
        self._worker = worker

    def stop(self) -> None:
        self._stop_event.set()

    def set_commands(self) -> None:
        self._gateway.set_bot_commands_from_file(self._cfg.commands_file)

    async def run_forever(self) -> None:
        offset = self._offset_store.read()
        _LOGGER.info("Починаю polling Telegram (offset=%s)", offset)

        while not self._stop_event.is_set():
            try:
                updates = await asyncio.to_thread(self._gateway.get_updates, offset)
            except Exception:
                _LOGGER.error("Помилка getUpdates, повтор через 2с", exc_info=True)
                await asyncio.sleep(2)
                continue

            if not updates:
                continue

            valid_updates = [u for u in updates if int(u.get("update_id", 0)) > 0]
            if not valid_updates:
                continue

            results = await asyncio.gather(
                *(self._handle_update(u) for u in valid_updates),
                return_exceptions=True,
            )

            update_ids = [int(u.get("update_id", 0)) for u in valid_updates]
            new_offset = self._compute_new_offset(offset, update_ids, results)

            if new_offset != offset:
                offset = new_offset
                self._offset_store.write(offset)

    @staticmethod
    def _compute_new_offset(current_offset: int | None, update_ids: list[int], results: list) -> int | None:
        """
        Чиста функція (винесена окремо, щоб її можна було перевірити без
        мережі): offset просувається аж до (не включно) першого update'у
        в пачці, що впав — так, як описано у docstring класу вище.
        """
        new_offset = current_offset
        for update_id, result in zip(update_ids, results):
            if isinstance(result, Exception) or result is False:
                _LOGGER.error("Update %s не оброблено — offset не рухаю далі нього", update_id)
                break
            new_offset = update_id + 1
        return new_offset

    async def _handle_update(self, update: dict) -> bool:
        t0 = time.monotonic()
        try:
            event = self._gateway.extract_event(update)
        except Exception:
            _LOGGER.error("Помилка розбору update", exc_info=True)
            return False

        if event is None:
            # Тип update, який ми свідомо не обробляємо (наприклад, my_chat_member) — не помилка.
            return True

        plugin = self._registry.find_for_event(event)
        if plugin is None:
            _LOGGER.debug("Жоден плагін не обробив подію від chat_id=%s", event.source.chat_id)
        else:
            try:
                result = await plugin.run(event)
            except Exception:
                _LOGGER.error("Помилка плагіна %s у run()", plugin.name, exc_info=True)
                await self._safe_send(event.source.chat_id, "Сталася помилка обробки запиту. Спробуйте пізніше.")
                return False

            sent_ids = await self._send_outputs(result.outputs)

            if result.onec_submission is not None:
                if self._worker is None:
                    _LOGGER.error("OneCWorker не підключено — запит плагіна %s втрачено", plugin.name)
                else:
                    # Повідомлення, що запустило дію (клік кнопки / дані веб-аппа),
                    # і будь-які службові тексти, надіслані разом із запитом
                    # (напр. "⏳ Формую...") — прибираються після успішної
                    # доставки результату, щоб у чаті лишились лише файли/відповіді.
                    cleanup_ids = [mid for mid in [event.message_id_int, *sent_ids] if mid]
                    request = OneCRequest(
                        request_id=uuid4().hex,
                        plugin_name=plugin.name,
                        chat_id=event.source.chat_id,
                        command_name=result.onec_submission.command_name,
                        params=result.onec_submission.params,
                        cleanup_message_ids=cleanup_ids,
                    )
                    log_request(
                        _REQUEST_LOGGER,
                        request_id=request.request_id,
                        chat_id=event.source.chat_id,
                        username=event.source.username,
                        user_id=event.source.user_id,
                        command_name=request.command_name,
                        params=request.params,
                    )
                    self._worker.submit(request)

        # Пост-обробка — так само, як у vps_input_msg_telegram: реакція 👀 на
        # message-подіях, підтвердження на callback_query. Лише після успіху.
        # Виняток — повідомлення з web_app_data: Telegram відхиляє на них
        # setMessageReaction (MESSAGE_ID_INVALID), тому навіть не намагаємось.
        if event.event_type == "message" and not event.has_web_app_data:
            await asyncio.to_thread(self._gateway.set_message_reaction_eyes, event.source.chat_id, event.message_id_int)
        elif event.event_type == "callback_query" and event.callback_query_id:
            await asyncio.to_thread(self._gateway.answer_callback_query, event.callback_query_id)

        total_elapsed = time.monotonic() - t0
        if total_elapsed >= 1.0:
            _LOGGER.debug("_handle_update зайняв %.2fс (chat_id=%s, update_id=%s)", total_elapsed, event.source.chat_id, event.update_id)

        return True

    async def deliver_onec_result(self, result: OneCResult) -> None:
        """Викликається з OneCWorker (через run_coroutine_threadsafe), коли задача в 1С завершена."""
        _LOGGER.debug("deliver_onec_result: отримано результат request_id=%s chat_id=%s", result.request_id, result.chat_id)

        plugin = self._registry.get(result.plugin_name)
        if plugin is None:
            _LOGGER.error("Немає плагіна '%s' для результату 1С request_id=%s", result.plugin_name, result.request_id)
            return

        event = OneCResultEvent(
            source=Source(system="telegram", chat_id=result.chat_id),
            request_id=result.request_id,
            command_name=result.command_name,
            data=result.data,
            error=result.error,
            request_params=result.request_params,
        )
        try:
            plugin_result = await plugin.on_onec_result(event)
        except Exception:
            _LOGGER.error("Помилка плагіна %s у on_onec_result() (request_id=%s)", plugin.name, result.request_id, exc_info=True)
            await self._safe_send(result.chat_id, "Сталася помилка при формуванні відповіді.")
            return

        await self._send_outputs(plugin_result.outputs)
        _LOGGER.debug("deliver_onec_result: завершено, output(ів)=%s (request_id=%s)", len(plugin_result.outputs), result.request_id)

        # Прибираємо службові повідомлення ЛИШЕ якщо результат дійсно успішний
        # (result.error is None) — при помилці/таймауті лишаємо все як є, щоб
        # не втратити контекст для діагностики.
        if result.error is None and result.cleanup_message_ids:
            for message_id in result.cleanup_message_ids:
                await asyncio.to_thread(self._gateway.delete_message, result.chat_id, message_id)

    async def _send_outputs(self, outputs: list[PluginOutput]) -> list[int]:
        sent_ids: list[int] = []
        for output in outputs:
            try:
                if output.kind == "text":
                    reply_markup = output.reply_markup.to_dict() if output.reply_markup is not None else None
                    message_id = await asyncio.to_thread(self._gateway.send_message, output.chat_id, output.text or "", reply_markup)
                elif output.kind == "document" and output.file_path:
                    message_id = await asyncio.to_thread(self._gateway.send_document, output.chat_id, output.file_path, output.caption)
                else:
                    _LOGGER.warning("Невідомий тип виводу плагіна: %s", output.kind)
                    continue
                if message_id:
                    sent_ids.append(message_id)
            except Exception:
                _LOGGER.error("Не вдалося відправити повідомлення в Telegram (chat_id=%s)", output.chat_id, exc_info=True)
        return sent_ids

    async def _safe_send(self, chat_id: str, text: str) -> None:
        try:
            await asyncio.to_thread(self._gateway.send_message, chat_id, text)
        except Exception:
            _LOGGER.error("Не вдалося відправити повідомлення про помилку (chat_id=%s)", chat_id, exc_info=True)
