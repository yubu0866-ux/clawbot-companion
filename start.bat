@echo off
rem clawbot-companion launcher for Windows (mirrors start.sh)
rem Usage: copy config.example.json to config.json (and persona/facts likewise),
rem        fill in values, set your API key env var, then run:  start.bat
setlocal
cd /d "%~dp0"

rem Optional: load local env file if present (.env is git-ignored), KEY=VALUE per line
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do if not "%%B"=="" set "%%A=%%B"
)

rem Timezone: config.json "timezone" is authoritative for the app; TZ here only aligns log timestamps.
if not defined CLAWBOT_TZ set "CLAWBOT_TZ=Asia/Shanghai"
set "TZ=%CLAWBOT_TZ%"

rem Force UTF-8 for Python I/O so Chinese logs/content survive any console codepage.
set "PYTHONUTF8=1"

rem API key is read from the environment variable named by config llm.api_key_env - never hard-code it here.
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -u bridge.py serve
) else (
  python -u bridge.py serve
)
endlocal
