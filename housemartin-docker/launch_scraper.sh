#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# launch_scraper.sh
# Called by Synology Task Scheduler at 7am daily.
# Paths are read from .env — no hardcoded values in this file.
# ─────────────────────────────────────────────────────────────────────────────

# Load .env from the same directory as this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/.env" ]; then
    set -a
    source "${SCRIPT_DIR}/.env"
    set +a
else
    echo "ERROR: .env not found at ${SCRIPT_DIR}/.env" >&2
    exit 1
fi

# Both paths must be set in .env
if [ -z "$COMPOSE_DIR" ] || [ -z "$DROPBOX_DIR" ]; then
    echo "ERROR: COMPOSE_DIR and DROPBOX_DIR must be set in .env" >&2
    exit 1
fi
LOG_DIR="${DROPBOX_DIR}/logs"
REPORTS_DIR="${DROPBOX_DIR}/reports"
SNAPSHOTS_DIR="${DROPBOX_DIR}/snapshots"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="${LOG_DIR}/scraper_${TIMESTAMP}.log"

mkdir -p "$LOG_DIR"

echo "[$TIMESTAMP] Starting housemartin-scraper container..." | tee -a "$LOG_FILE"

docker compose -f "${COMPOSE_DIR}/docker-compose.yml" run --rm housemartin-scraper \
    >> "$LOG_FILE" 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$(date +"%H:%M:%S")] ✓ Scraper finished successfully." | tee -a "$LOG_FILE"
else
    echo "[$(date +"%H:%M:%S")] ✗ Scraper failed with exit code ${EXIT_CODE}." | tee -a "$LOG_FILE"
fi

# ── Sync output files to Cloud Sync ──────────────────────────────────────────
# Files written inside the container don't reliably trigger inotify on the NAS.
# We touch them here (outside the container) to force Cloud Sync to notice them.
sleep 2

# Touch timestamped reports and snapshots
find "$REPORTS_DIR" -name "hm_report_2*.html" -exec touch {} \;
find "$SNAPSHOTS_DIR" -name "*.txt" -exec touch {} \;
find "$SNAPSHOTS_DIR" -name "*.csv" -exec touch {} \;
touch "${DROPBOX_DIR}/hm_pp_state_history.csv"
touch "${DROPBOX_DIR}/hmfund_quotes.csv"
touch "${DROPBOX_DIR}/hmfund_transactions_seed.csv"
touch "${DROPBOX_DIR}/hm_pp_state.json"
touch "${DROPBOX_DIR}/hm_pp_state.backup.json"

# hm_report_latest.html: delete and recreate so Cloud Sync sees it as changed
# (touching an existing synced file doesn't always trigger an upload)
LATEST="${REPORTS_DIR}/hm_report_latest.html"
NEWEST=$(ls -t "${REPORTS_DIR}"/hm_report_2*.html 2>/dev/null | head -1)
if [ -n "$NEWEST" ] && [ -f "$NEWEST" ]; then
    rm -f "$LATEST"
    cp "$NEWEST" "$LATEST"
    echo "[$(date +"%H:%M:%S")] Refreshed hm_report_latest.html from $(basename $NEWEST)" | tee -a "$LOG_FILE"
fi

echo "[$(date +"%H:%M:%S")] Output files touched for Cloud Sync." | tee -a "$LOG_FILE"

# Keep only the last 30 log files
ls -t "${LOG_DIR}"/scraper_*.log 2>/dev/null | tail -n +31 | xargs rm -f

# ── Run Housemartin price history + normalisation ─────────────────────────────
echo "[$(date +"%H:%M:%S")] Starting price history scraper..." | tee -a "$LOG_FILE"

docker compose -f "${COMPOSE_DIR}/docker-compose.yml" run --rm \
    -e OUTPUT_DIR=/dropbox_data \
    housemartin-scraper \
    python -u /dropbox_data/housemartin_price_history.py \
    >> "$LOG_FILE" 2>&1

PH_EXIT=$?

if [ $PH_EXIT -eq 0 ]; then
    echo "[$(date +"%H:%M:%S")] ✓ Price history finished successfully." | tee -a "$LOG_FILE"
else
    echo "[$(date +"%H:%M:%S")] ✗ Price history failed with exit code ${PH_EXIT}." | tee -a "$LOG_FILE"
fi

# Touch price history files so Cloud Sync notices them
sleep 2
find "$DROPBOX_DIR" -maxdepth 1 -name "housemartin_price_history*" -exec touch {} \;
echo "[$(date +"%H:%M:%S")] Price history files touched for Cloud Sync." | tee -a "$LOG_FILE"

# Exit with failure if either job failed
[ $EXIT_CODE -ne 0 ] && exit $EXIT_CODE
exit $PH_EXIT
