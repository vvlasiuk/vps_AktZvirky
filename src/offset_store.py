from __future__ import annotations

from pathlib import Path


class OffsetStore:
    """
    Персистентний offset для getUpdates — той самий підхід, що й у
    vps_input_msg_telegram (файл var/offset.txt): offset записується на диск
    лише ПІСЛЯ успішної обробки update'у, тому при рестарті процесу жодне
    повідомлення не губиться (Telegram просто поверне його ще раз).
    """

    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> int | None:
        if not self._path.exists():
            return None
        raw = self._path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def write(self, offset: int) -> None:
        self._path.write_text(str(offset), encoding="utf-8")
