@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m btmix web
) else (
  python -m btmix web
)
pause
