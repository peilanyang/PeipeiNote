@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    uv venv --python ">=3.11" .venv || exit /b 1
)
uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt || exit /b 1
if not exist "uploads" mkdir "uploads"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log
