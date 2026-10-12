#!/usr/bin/env bash
# Headless scheduled run of the nepremicnine-scout skill, once per alert in config/alerts/.
# Invoked by cron. Pass alert names as arguments to run only those.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
mkdir -p logs
nep=(uv run -q .claude/skills/nepremicnine-scout/nep.py)

# Skip if a previous run is still going.
exec 9>logs/.lock
flock -n 9 || { echo "$(date -Is) previous run still active, skipping"; exit 0; }

status=0
while IFS=$'\t' read -r alert state; do
  [ $# -eq 0 ] || [[ " $* " == *" $alert "* ]] || continue
  # An alert without a webhook or criteria would re-evaluate the same listings every run.
  if [ "$state" != ready ]; then
    echo "$(date -Is) skipping $alert ($state)"
    continue
  fi
  log="logs/$(date +%Y-%m-%d_%H%M)_$alert.log"
  claude -p "Run the nepremicnine-scout skill for alert \`$alert\`." \
    --model sonnet \
    --permission-mode dontAsk \
    --max-budget-usd "${SCOUT_MAX_BUDGET_USD:-2}" \
    </dev/null >"$log" 2>&1 || status=$?
done < <("${nep[@]}" alerts)

# Keep two weeks of logs.
find logs -name '*.log' -mtime +14 -delete
exit $status
