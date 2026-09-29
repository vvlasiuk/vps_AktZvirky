from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.plugins.akt_zvirky.plugin import AktZvirkyPlugin
from src.plugins.base import IncomingEvent, Source


def _event(text: str = "", command_name: str = "", command_params: dict | None = None) -> IncomingEvent:
    return IncomingEvent(
        source=Source(system="telegram", chat_id="1"),
        text=text,
        command_name=command_name,
        command_params=command_params or {},
    )


def test_matches_menu_buttons_with_high_score(tmp_path):
    plugin = AktZvirkyPlugin(temp_files_dir=tmp_path)
    for text in ("/start", "🧾 Акт звірки", "📅 Звірка за поточний місяць", "⬅️ До головного меню"):
        decision = plugin.matches(_event(text=text))
        assert decision.should_run is True
        assert decision.score == 10.0


def test_matches_webapp_data_with_high_score(tmp_path):
    plugin = AktZvirkyPlugin(temp_files_dir=tmp_path)
    decision = plugin.matches(
        _event(command_name="AKTZVIRKY_PERIOD", command_params={"date_start": "01.09.2026", "date_end": "28.09.2026"})
    )
    assert decision.should_run is True
    assert decision.score == 10.0


def test_matches_unrelated_text_falls_back_low_score(tmp_path):
    plugin = AktZvirkyPlugin(temp_files_dir=tmp_path)
    decision = plugin.matches(_event(text="привіт як справи"))
    assert decision.should_run is True
    assert decision.score == 1.0


def test_registry_picks_highest_score_when_multiple_plugins_match():
    from src.plugins.base import MatchDecision, MessagePlugin, PluginResult
    from src.plugins.registry import PluginRegistry

    class LowPriorityPlugin(MessagePlugin):
        name = "low"

        def matches(self, event):
            return MatchDecision(should_run=True, score=1.0)

        async def run(self, event):
            return PluginResult()

    class HighPriorityPlugin(MessagePlugin):
        name = "high"

        def matches(self, event):
            return MatchDecision(should_run=True, score=5.0)

        async def run(self, event):
            return PluginResult()

    registry = PluginRegistry([LowPriorityPlugin(), HighPriorityPlugin()])
    chosen = registry.find_for_event(_event(text="anything"))
    assert chosen.name == "high"
