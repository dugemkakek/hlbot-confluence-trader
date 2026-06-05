@echo off
REM === HLBot Starter ===
REM Starts the HL trading bot on port 8000
REM Usage: double-click this file, or run from terminal
REM Requires: Python 3.11+ on PATH (or py launcher)

REM Move to the project root (parent of this .bat file's directory)
cd /d "%~dp0"

REM Kill any existing process on port 8000
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    echo Killing existing process on port 8000: %%a
    taskkill /F /PID %%a >nul 2>&1
)

REM Start the bot
echo Starting HLBot...
start "HLBot" cmd /k "py -3 -m uvicorn src.api.main:create_app --factory --host 0.0.0.0 --port 8000"

timeout /t 3 /nobreak >nul
curl -s http://localhost:8000/health && echo " — HLBot is up!" || echo "HLBot failed to start"
