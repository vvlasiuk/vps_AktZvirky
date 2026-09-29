from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    telegram_token: str
    telegram_poll_timeout: int
    admin_chat_id: str
    offset_file: Path
    commands_file: Path

    onec_server: str
    onec_ref: str
    onec_file_path: str
    onec_user: str
    onec_password: str
    onec_connector_prog_id: str
    onec_reconnect_interval_sec: float

    poll_interval_sec: float
    task_timeout_sec: float
    loop_sleep_sec: float

    db_path: Path
    temp_files_dir: Path

    log_level: str
    log_max_bytes: int
    log_backup_count: int
    log_file: Path

    # Окремий аудит-лог запитів до 1С (хто/коли/яку команду/з якими
    # параметрами) — не залежить від LOG_LEVEL, пишеться завжди.
    request_log_file: Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Не вказана обов'язкова змінна середовища: {name}")
    return value


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)).strip())


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)).strip())


def _resolve_path(raw: str, project_root: Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else project_root / path


def load_config(env_path: str | None, log_path_override: str | None) -> AppConfig:
    # src/config.py -> корінь проекту на один рівень вище src/
    project_root = Path(__file__).resolve().parent.parent
    fallback_env = project_root / ".env"
    resolved_env = Path(env_path).expanduser().resolve() if env_path else fallback_env

    if resolved_env.exists():
        load_dotenv(resolved_env)

    log_file = (
        Path(log_path_override).expanduser().resolve()
        if log_path_override
        else _resolve_path(os.getenv("LOG_FILE", "logs/service.log"), project_root)
    )

    return AppConfig(
        telegram_token=_required("TELEGRAM_TOKEN"),
        telegram_poll_timeout=_int("TELEGRAM_POLL_TIMEOUT", 30),
        admin_chat_id=os.getenv("ADMIN_CHAT_ID", "").strip(),
        offset_file=_resolve_path(os.getenv("TELEGRAM_OFFSET_FILE", "storage/offset.txt"), project_root),
        commands_file=_resolve_path(os.getenv("COMMANDS_FILE", "commands.json"), project_root),
        onec_server=os.getenv("ONEC_SERVER", ""),
        onec_ref=os.getenv("ONEC_REF", ""),
        onec_file_path=os.getenv("ONEC_FILE_PATH", ""),
        onec_user=_required("ONEC_USER"),
        onec_password=_required("ONEC_PASSWORD"),
        onec_connector_prog_id=os.getenv("ONEC_CONNECTOR_PROG_ID", "V83.COMConnector"),
        onec_reconnect_interval_sec=_float("ONEC_RECONNECT_INTERVAL_SEC", 60.0),
        poll_interval_sec=_float("POLL_INTERVAL_SEC", 2.0),
        task_timeout_sec=_float("TASK_TIMEOUT_SEC", 300.0),
        loop_sleep_sec=_float("LOOP_SLEEP_SEC", 0.2),
        db_path=_resolve_path(os.getenv("DB_PATH", "storage/state.sqlite3"), project_root),
        temp_files_dir=_resolve_path(os.getenv("TEMP_FILES_DIR", "storage/tmp"), project_root),
        log_level=os.getenv("LOG_LEVEL", "ERROR").upper(),
        log_max_bytes=_int("LOG_MAX_BYTES", 1_048_576),
        log_backup_count=_int("LOG_BACKUP_COUNT", 5),
        log_file=log_file,
        request_log_file=_resolve_path(os.getenv("REQUEST_LOG_FILE", "logs/requests.log"), project_root),
    )
