from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "vps_akt_zvirky"


def setup_logger(level: str, log_file: Path, max_bytes: int, backup_count: int) -> logging.Logger:
    """
    Один логер, два хендлери РІЗНОГО рівня деталізації — це і є вся
    "магія", яка раніше замінялась купою розкиданих print():

    - файл (logs/service.log) — рівно LOG_LEVEL з .env (типово ERROR),
      щоб не роздувати файл рутинними подіями на проді;
    - консоль — завжди щонайменше INFO, незалежно від LOG_LEVEL, щоб
      старт/зупинка/життєвий цикл були видні в інтерактивному вікні
      (саме це просили: "виведи хоть що він стартує"), і водночас
      детальніше (DEBUG), якщо LOG_LEVEL явно виставлено в DEBUG —
      для разової діагностики без окремого коду.

    Завдяки цьому logger.info()/error()/... сам по собі йде і в консоль,
    і у файл (коли рівень дозволяє) — окремих print() для дублювання
    більше не потрібно ніде, крім однієї точки в main.py ДО того, як
    цей логер взагалі створено.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    file_level = getattr(logging, level, logging.ERROR)
    logger.setLevel(min(file_level, logging.INFO))
    logger.propagate = False

    log_file.parent.mkdir(parents=True, exist_ok=True)

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")

    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(min(file_level, logging.INFO))
    console.setFormatter(formatter)
    logger.addHandler(console)

    return logger
