#!/usr/bin/env bash
# Headless scheduled run of the nepremicnine-scout skill. Invoked by cron / systemd.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
mkdir -p logs

# Without a webhook every run would re-evaluate the same listings, so don't start.
grep -q '^DISCORD_WEBHOOK_URL=https://discord' .env 2>/dev/null || { echo "DISCORD_WEBHOOK_URL missing in .env" >&2; exit 1; }

[ -f config/criteria.md ] || { echo "config/criteria.md missing (copy config/criteria.example.md)" >&2; exit 1; }

# Skip if a previous run is still going.
exec 9>logs/.lock
flock -n 9 || { echo "$(date -Is) previous run still active, skipping"; exit 0; }

log="logs/$(date +%Y-%m-%d_%H%M).log"
status=0
claude -p "Run the nepremicnine-scout skill." \
  --model sonnet \
  --permission-mode dontAsk \
  --max-budget-usd "${SCOUT_MAX_BUDGET_USD:-2}" \
  >"$log" 2>&1 || status=$?

# Keep two weeks of logs.
find logs -name '*.log' -mtime +14 -delete
exit $status
