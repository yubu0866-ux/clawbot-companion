#!/usr/bin/env bash
# clawbot-companion launcher (example)
# Usage: cp config.example.json config.json  (and persona/facts likewise), fill in values,
#        export your API key env var, then run:  ./start.sh
set -euo pipefail
cd "$(dirname "$0")"

# Optional: load local env file if present (.env is git-ignored)
if [[ -f .env ]]; then set -a; . ./.env; set +a; fi

# Timezone: config.json "timezone" is authoritative for the app; TZ here only aligns log timestamps.
export TZ="${CLAWBOT_TZ:-Asia/Shanghai}"

# API key is read from the environment variable named by config llm.api_key_env — never hard-code it here.
exec python3 -u bridge.py serve
