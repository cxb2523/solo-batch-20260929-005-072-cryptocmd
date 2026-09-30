#!/usr/bin/env bash
#
# verify.sh - gate for the dispatch/exchange refactor.
#
# 1. starts the local server in deterministic offline (fixture) mode
# 2. GET /health must return exactly "ok"
# 3. GET /ohlcv must be byte-identical to the frozen baseline CSV
# 4. the (non-integration) regression suite must pass
#
# Exits non-zero on any failure. Optional env: HOST, PORT, PYTHON.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8042}"
PYTHON="${PYTHON:-python}"
BASE_URL="http://${HOST}:${PORT}"
BASELINE="$REPO_ROOT/tests/fixtures/bitstamp_btcusd_baseline.csv"
WORK_DIR="$(mktemp -d)"
ACTUAL="$WORK_DIR/ohlcv.csv"
LOG="$WORK_DIR/server.log"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

echo ">> starting cryptocmd server (fixtures) on $BASE_URL"
"$PYTHON" -m cryptocmd.server --fixtures --host "$HOST" --port "$PORT" \
  --exchange bitstamp --coin btc --fiat usd >"$LOG" 2>&1 &
SERVER_PID=$!

echo ">> waiting for /health"
ready=0
for _ in $(seq 1 50); do
  if body="$(curl -fsS "$BASE_URL/health" 2>/dev/null)" && [[ "$body" == "ok" ]]; then
    ready=1
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "!! server exited early; log follows"
    cat "$LOG" >&2
    exit 1
  fi
  sleep 0.2
done

if [[ "$ready" -ne 1 ]]; then
  echo "!! /health never returned ok" >&2
  cat "$LOG" >&2
  exit 1
fi
echo "   /health -> ok"

echo ">> fetching /ohlcv and diffing against baseline"
curl -fsS "$BASE_URL/ohlcv?exchange=bitstamp&coin=btc&fiat=usd&start=15-12-2023&end=15-01-2024" \
  -o "$ACTUAL"

if ! cmp -s "$ACTUAL" "$BASELINE"; then
  echo "!! /ohlcv differs from baseline CSV" >&2
  diff -u "$BASELINE" "$ACTUAL" >&2 || true
  exit 1
fi
echo "   byte-identical to $BASELINE"

echo ">> running regression tests (non-integration)"
"$PYTHON" -m pytest -m "not integration" -q

echo ">> OK: health, byte-diff and regression suite passed"
