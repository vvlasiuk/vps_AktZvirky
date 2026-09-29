@echo off
title vps_AktZvirky
.venv\Scripts\python main.py --env .env

rem Вікно лишається відкритим після зупинки/падіння скрипта, щоб було видно
rem стартові повідомлення й текст помилки, а не лише миттєве закриття консолі.
echo.
echo vps_AktZvirky завершив роботу (код виходу: %errorlevel%). Натисніть будь-яку клавішу для закриття...
pause >nul
