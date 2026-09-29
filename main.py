from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from datetime import datetime
from pathlib import Path

from src.config import load_config
from src.logger_setup import setup_logger
from src.onec_worker import OneCWorker
from src.plugins.akt_zvirky.plugin import AktZvirkyPlugin
from src.plugins.registry import PluginRegistry
from src.request_log import setup_request_logger
from src.state_store import StateStore
from src.telegram_app import TelegramApp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="vps_AktZvirky — Telegram-бот акта звірки з прямою інтеграцією 1С (без RabbitMQ)")
    parser.add_argument("--env", dest="env_path", default=None, help="Шлях до env-файлу")
    parser.add_argument("--log-path", dest="log_path", default=None, help="Шлях до лог-файлу")
    return parser.parse_args()


async def run() -> None:
    args = parse_args()
    cfg = load_config(args.env_path, args.log_path)
    logger = setup_logger(cfg.log_level, cfg.log_file, cfg.log_max_bytes, cfg.log_backup_count)

    # logger.info() тут іде і в консоль (хендлер консолі завжди >= INFO,
    # див. logger_setup.py), і у файл — окремого print() для дублювання
    # більше не потрібно.
    logger.info("vps_AktZvirky: конфігурацію завантажено (LOG_LEVEL=%s, лог-файл: %s)", cfg.log_level, cfg.log_file)

    # Окремий лог запитів (хто/коли/яку команду/з якими параметрами) —
    # незалежний від LOG_LEVEL, пишеться завжди в окремий файл.
    setup_request_logger(cfg.request_log_file, cfg.log_max_bytes, cfg.log_backup_count)
    logger.info("Лог запитів: %s", cfg.request_log_file)

    state_store = StateStore(cfg.db_path)
    removed = state_store.cleanup_old()
    if removed:
        logger.info("Очищено %s застарілих записів у state_store", removed)

    registry = PluginRegistry([AktZvirkyPlugin(temp_files_dir=cfg.temp_files_dir)])

    telegram_app = TelegramApp(cfg, registry)
    telegram_app.set_commands()

    loop = asyncio.get_running_loop()
    worker = OneCWorker(cfg, logger, state_store, loop, telegram_app.deliver_onec_result)
    telegram_app.attach_worker(worker)
    worker.start()

    logger.info("vps_AktZvirky: запущено, слухаю Telegram (Ctrl+C для зупинки)")

    try:
        await telegram_app.run_forever()
    finally:
        logger.info("vps_AktZvirky: зупинка...")
        worker.stop()
        state_store.close()


def _write_fallback_startup_error(exc: BaseException) -> None:
    """
    Викликається, коли впало ДО того, як створено logger (наприклад, помилка
    в .env) — logger ще не існує, а якщо скрипт запущено без консолі
    (служба/планувальник), stderr також нікуди не потрапляє. Пишемо
    traceback у окремий файл поряд зі скриптом, щоб причину можна було
    побачити навіть у такому випадку.
    """
    try:
        project_root = Path(__file__).resolve().parent
        fallback_path = project_root / "logs" / "startup_error.log"
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        with fallback_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {datetime.now().isoformat()} ===\n")
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass  # немає сенсу падати ще й тут — це вже запасний варіант діагностики


def main() -> int:
    print("vps_AktZvirky: стартую...", flush=True)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr, flush=True)
        _write_fallback_startup_error(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
