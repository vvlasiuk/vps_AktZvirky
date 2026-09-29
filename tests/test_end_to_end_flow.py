"""
Наскрізна перевірка сценаріїв використання без мережі й без 1С:
викликає plugin.run()/on_onec_result() напряму (те, що в проді викликає
telegram_app.py), як робив би реальний диспетчер. pytest-asyncio не
використовується навмисно (щоб не тягнути зайву залежність лише для
тестів) — корутини запускаються через asyncio.run().
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.plugins.akt_zvirky.plugin import AktZvirkyPlugin
from src.plugins.base import IncomingEvent, OneCResultEvent, Source


def _plugin(tmp_path: Path) -> AktZvirkyPlugin:
    return AktZvirkyPlugin(temp_files_dir=tmp_path / "tmp_files")


def _event(text: str = "", command_name: str = "", command_params: dict | None = None) -> IncomingEvent:
    return IncomingEvent(
        source=Source(system="telegram", chat_id="777"),
        text=text,
        command_name=command_name,
        command_params=command_params or {},
    )


def test_scenario_start_shows_main_menu_with_real_keyboard(tmp_path):
    plugin = _plugin(tmp_path)
    result = asyncio.run(plugin.run(_event(text="/start")))
    assert len(result.outputs) == 1
    output = result.outputs[0]
    assert output.kind == "text"
    assert output.reply_markup is not None
    assert result.onec_submission is None


def test_scenario_reconcile_button_shows_period_menu(tmp_path):
    plugin = _plugin(tmp_path)
    result = asyncio.run(plugin.run(_event(text="🧾 Акт звірки")))
    assert "період" in result.outputs[0].text.lower()
    assert result.onec_submission is None


def test_scenario_pick_previous_month_submits_to_onec(tmp_path):
    plugin = _plugin(tmp_path)
    result = asyncio.run(plugin.run(_event(text="📆 Звірка за попередній місяць")))

    # користувач одразу отримує "формується" — не чекає на 1С синхронно
    assert len(result.outputs) == 1
    assert "формую" in result.outputs[0].text.lower()

    assert result.onec_submission is not None
    assert result.onec_submission.command_name == "AKTZVIRKY"
    assert set(result.onec_submission.params) == {"chat_id", "date_start", "date_end"}
    assert result.onec_submission.params["chat_id"] == "777"


def test_scenario_custom_period_via_webapp_submits_to_onec(tmp_path):
    """
    Реальний контракт веб-аппа (vps_telegram_webapp_document_period), як його
    розбирає TelegramGateway._extract_command_from_web_app_data і як його
    очікує demo_bot-плагін у vps_dispatcher: command_name=AKTZVIRKY_PERIOD +
    command_params з датами — а НЕ голий {"date_start":...} на верхньому рівні.
    """
    plugin = _plugin(tmp_path)
    event = _event(
        command_name="AKTZVIRKY_PERIOD",
        command_params={"date_start": "01.03.2026", "date_end": "15.03.2026"},
    )
    result = asyncio.run(plugin.run(event))

    assert result.onec_submission is not None
    assert result.onec_submission.params["date_start"] == "01.03.2026"
    assert result.onec_submission.params["date_end"] == "15.03.2026"


def test_scenario_webapp_missing_dates_does_not_submit(tmp_path):
    plugin = _plugin(tmp_path)
    event = _event(command_name="AKTZVIRKY_PERIOD", command_params={"unexpected": "shape"})
    result = asyncio.run(plugin.run(event))

    assert result.onec_submission is None
    assert "не вдалося визначити період" in result.outputs[0].text.lower()


def test_scenario_onec_success_with_file_delivers_document(tmp_path):
    """
    request_params (date_start/date_end вихідного запиту) мають потрапити в
    підпис до файлу — щоб при кількох одночасних запитах від одного chat_id
    було видно, за який саме період прийшла відповідь.
    """
    plugin = _plugin(tmp_path)
    file_bytes = b"fake pdf content"
    onec_event = OneCResultEvent(
        source=Source(system="telegram", chat_id="777"),
        request_id="req-abc",
        command_name="AKTZVIRKY_COMPLETED",
        data={
            "text": "Акт звірки за 09.2026",
            "file": [
                {
                    "file_name": "akt_zvirky_09_2026.pdf",
                    "file_base64": base64.b64encode(file_bytes).decode("ascii"),
                }
            ],
        },
        error=None,
        request_params={"date_start": "01.09.2026", "date_end": "28.09.2026"},
    )
    result = asyncio.run(plugin.on_onec_result(onec_event))

    assert len(result.outputs) == 1
    output = result.outputs[0]
    assert output.kind == "document"
    assert "01.09.2026" in output.caption
    assert "28.09.2026" in output.caption
    assert "Акт звірки за 09.2026" in output.caption
    saved = Path(output.file_path)
    assert saved.exists()
    assert saved.read_bytes() == file_bytes


def test_scenario_onec_success_without_file_sends_text(tmp_path):
    plugin = _plugin(tmp_path)
    onec_event = OneCResultEvent(
        source=Source(system="telegram", chat_id="777"),
        request_id="req-xyz",
        command_name="AKTZVIRKY_COMPLETED",
        data={"text": "Даних за цей період немає."},
        error=None,
    )
    result = asyncio.run(plugin.on_onec_result(onec_event))
    assert result.outputs[0].kind == "text"
    assert "немає" in result.outputs[0].text.lower()


def test_scenario_onec_error_sends_error_text(tmp_path):
    plugin = _plugin(tmp_path)
    onec_event = OneCResultEvent(
        source=Source(system="telegram", chat_id="777"),
        request_id="req-err",
        command_name=None,
        data=None,
        error="Партнер не знайдений",
        request_params={"date_start": "01.01.2026", "date_end": "31.01.2026"},
    )
    result = asyncio.run(plugin.on_onec_result(onec_event))
    assert "помилка" in result.outputs[0].text.lower()
    assert "партнер не знайдений" in result.outputs[0].text.lower()
    assert "01.01.2026" in result.outputs[0].text
    assert "31.01.2026" in result.outputs[0].text


def test_scenario_period_label_omitted_when_request_params_missing(tmp_path):
    """Немає request_params (напр. старий/невідомий запит) — не падаємо, просто без періоду в тексті."""
    plugin = _plugin(tmp_path)
    onec_event = OneCResultEvent(
        source=Source(system="telegram", chat_id="777"),
        request_id="req-noparams",
        command_name="AKTZVIRKY_COMPLETED",
        data={"text": "Готово."},
        error=None,
    )
    result = asyncio.run(plugin.on_onec_result(onec_event))
    assert "готово" in result.outputs[0].text.lower()
    assert "—" not in result.outputs[0].text  # роздільник періоду відсутній


def test_scenario_onec_timeout_reported_as_error_text(tmp_path):
    plugin = _plugin(tmp_path)
    onec_event = OneCResultEvent(
        source=Source(system="telegram", chat_id="777"),
        request_id="req-timeout",
        command_name=None,
        data=None,
        error="Таймаут очікування відповіді від 1С",
    )
    result = asyncio.run(plugin.on_onec_result(onec_event))
    assert "таймаут" in result.outputs[0].text.lower()


def test_scenario_malicious_file_name_is_sanitized(tmp_path):
    """Захист від path traversal, якщо 1С раптом поверне ../../ у file_name."""
    plugin = _plugin(tmp_path)
    onec_event = OneCResultEvent(
        source=Source(system="telegram", chat_id="777"),
        request_id="req-evil",
        command_name="AKTZVIRKY_COMPLETED",
        data={
            "text": "test",
            "file": [
                {
                    "file_name": "../../../etc/evil.pdf",
                    "file_base64": base64.b64encode(b"x").decode("ascii"),
                }
            ],
        },
        error=None,
    )
    result = asyncio.run(plugin.on_onec_result(onec_event))
    saved_path = Path(result.outputs[0].file_path).resolve()
    temp_dir = (tmp_path / "tmp_files").resolve()
    assert temp_dir in saved_path.parents


def test_scenario_many_users_concurrently_get_independent_results(tmp_path):
    """Симуляція навантаження: N незалежних 'користувачів' одночасно проганяють повний цикл."""

    async def one_user(chat_id: str, plugin: AktZvirkyPlugin):
        menu = await plugin.run(_event(text="🧾 Акт звірки"))
        assert menu.onec_submission is None

        submit = await plugin.run(_event(text="🗓️ Звірка з початку року"))
        assert submit.onec_submission is not None

        result_event = OneCResultEvent(
            source=Source(system="telegram", chat_id=chat_id),
            request_id=f"req-{chat_id}",
            command_name="AKTZVIRKY_COMPLETED",
            data={"text": f"Готово для {chat_id}"},
            error=None,
        )
        delivered = await plugin.on_onec_result(result_event)
        return delivered.outputs[0].text

    async def main():
        plugin = _plugin(tmp_path)
        chat_ids = [str(i) for i in range(50)]
        results = await asyncio.gather(*(one_user(cid, plugin) for cid in chat_ids))
        return chat_ids, results

    chat_ids, results = asyncio.run(main())
    for chat_id, text in zip(chat_ids, results):
        assert chat_id in text
