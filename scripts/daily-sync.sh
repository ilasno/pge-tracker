#!/usr/bin/env bash
# Daily PG&E data sync script.
# Designed to be called by macOS launchd or cron.
#
# Usage:
#   ./scripts/daily-sync.sh                    # uses default config
#   ./scripts/daily-sync.sh /path/to/config.toml  # explicit config

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$SCRIPT_DIR/data/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/sync-$(date +%Y-%m-%d).log"
CONFIG_FLAG=""
if [[ -n "${1:-}" ]]; then
    CONFIG_FLAG="--config $1"
fi

echo "=== PG&E Sync: $(date) ===" >> "$LOG_FILE"

# Activate virtualenv if it exists
if [[ -f "$SCRIPT_DIR/.venv/bin/activate" ]]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

# Run the sync (incremental by default)
pge-tracker sync $CONFIG_FLAG >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "Exit code: $EXIT_CODE" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# Clean up old logs (keep 30 days)
find "$LOG_DIR" -name "sync-*.log" -mtime +30 -delete 2>/dev/null || true

exit $EXIT_CODE
