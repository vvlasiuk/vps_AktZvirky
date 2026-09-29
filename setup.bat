@echo off
setlocal

if not exist ".venv" (
    python -m venv .venv
)

call .venv\Scripts\python -m pip install --upgrade pip
call .venv\Scripts\pip install -r requirements.txt

echo.
echo Готово. Скопіюйте .env.example у .env і заповніть значення,
echo після чого запустіть run.bat (скопіюйте run.bat.example -> run.bat).
endlocal
