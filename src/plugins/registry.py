from __future__ import annotations

import logging

from .base import IncomingEvent, MessagePlugin

_LOGGER = logging.getLogger("vps_akt_zvirky")


class PluginRegistry:
    """
    Реєстр плагінів: для вхідної події обирає плагін з найвищим score серед
    тих, хто відповів should_run=True. Для результатів з 1С — звертається
    напряму за назвою плагіна, яку той сам вказав при відправці запиту.
    """

    def __init__(self, plugins: list[MessagePlugin]):
        self._plugins = plugins
        self._by_name = {plugin.name: plugin for plugin in plugins}
        if len(self._by_name) != len(plugins):
            raise ValueError("Знайдено плагіни з однаковим name — імена мають бути унікальними")

    def find_for_event(self, event: IncomingEvent) -> MessagePlugin | None:
        best: MessagePlugin | None = None
        best_score = -1.0
        for plugin in self._plugins:
            try:
                decision = plugin.matches(event)
            except Exception:
                _LOGGER.error("Плагін %s впав у matches()", plugin.name, exc_info=True)
                continue
            if decision.should_run and decision.score > best_score:
                best = plugin
                best_score = decision.score
        return best

    def get(self, name: str) -> MessagePlugin | None:
        return self._by_name.get(name)

    @property
    def plugins(self) -> list[MessagePlugin]:
        return list(self._plugins)
