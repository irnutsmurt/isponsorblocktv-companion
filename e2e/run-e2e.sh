#!/bin/bash
# Starts a throwaway companion (file log source, fake lounge API) and runs the browser test.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WORK:-$(mktemp -d)}
mkdir -p "$WORK/data" "$WORK/upstream"
: > "$WORK/upstream/isponsorblocktv.log"
. "$ROOT/.venv/bin/activate"
python "$ROOT/e2e/fake_lounge.py" & FAKE=$!
cd "$ROOT/backend"
DATA_DIR="$WORK/data" UPSTREAM_DATA_DIR="$WORK/upstream" LOG_SOURCE=file UPSTREAM_LOG_FILE="$WORK/upstream/isponsorblocktv.log" \
  UPSTREAM_TZ=UTC SETUP_TOKEN=e2e-setup-token-0123456789 PORT=8399 \
  YOUTUBE_LOUNGE_API_BASE=http://127.0.0.1:8299 ENRICHMENT_ENABLED=false AVAILABILITY_POLL_MINUTES=0 LOG_FORMAT=json \
  python -m companion > "$WORK/companion.log" 2>&1 & APP=$!
sleep 3
cd "$ROOT/e2e"
BASE=http://localhost:8399 SETUP_TOKEN=e2e-setup-token-0123456789 UPSTREAM_LOG_FILE="$WORK/upstream/isponsorblocktv.log" SHOTS="$WORK/shots" node run.mjs
RC=$?
kill $APP $FAKE 2>/dev/null
echo "work dir: $WORK"
exit $RC
