# clawbot-companion launcher for Windows PowerShell (mirrors start.sh)
# Usage: copy config.example.json to config.json (and persona/facts likewise),
#        fill in values, set your API key env var, then run:  .\start.ps1
# If scripts are blocked, run once:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Optional: load local env file if present (.env is git-ignored), KEY=VALUE per line
if (Test-Path ".env") {
    foreach ($line in Get-Content ".env" -Encoding UTF8) {
        if ($line -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
        }
    }
}

# Timezone: config.json "timezone" is authoritative for the app; TZ here only aligns log timestamps.
if (-not $env:CLAWBOT_TZ) { $env:CLAWBOT_TZ = "Asia/Shanghai" }
$env:TZ = $env:CLAWBOT_TZ

# Force UTF-8 for Python I/O so Chinese logs/content survive any console codepage.
$env:PYTHONUTF8 = "1"

# API key is read from the environment variable named by config llm.api_key_env - never hard-code it here.
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -u bridge.py serve
} else {
    & python -u bridge.py serve
}
