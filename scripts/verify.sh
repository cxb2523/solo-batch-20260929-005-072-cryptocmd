#!/usr/bin/env bash
# verify.sh — release gate for the exchange-dispatch build.
#
#   1. start cryptocmd server (fixture-backed, hermetic)
#   2. curl /health            -> must answer "ok"
#   3. curl /ohlcv             -> byte-diff against the baseline CSV
#   4. run the regression test suite
#
# Any mismatch or red test -> non-zero exit, this build does not ship.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${CRYPTOCMD_VERIFY_HOST:-127.0.0.1}"
PORT="${CRYPTOCMD_VERIFY_PORT:-8137}"
BASE_URL="http://${HOST}:${PORT}"
BASELINE="tests/baseline/bitstamp_btcusd_01-01-2024_10-01-2024.csv"
OHLCV_QUERY="exchange=bitstamp&symbol=btcusd&start=01-01-2024&end=10-01-2024"

PYTHON="${PYTHON:-python}"
OUT_CSV="$(mktemp)"
SERVER_LOG="$(mktemp)"
SERVER_PID=""

cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -f "$OUT_CSV" "$SERVER_LOG"
}
trap cleanup EXIT

fail() {
    echo "VERIFY FAILED: $*" >&2
    exit 1
}

# --- 1. start the server (fixture mode: no live network needed) -------------
export CRYPTOCMD_BITSTAMP_FIXTURE="$ROOT/tests/fixtures/bitstamp_btcusd.json"
"$PYTHON" -m cryptocmd.server --host "$HOST" --port "$PORT" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 40); do
    if curl -sf "${BASE_URL}/health" >/dev/null 2>&1; then
        break
    fi
    sleep 0.25
done

# --- 2. /health must answer "ok" ---------------------------------------------
HEALTH="$(curl -sf "${BASE_URL}/health")" || fail "/health unreachable (server log: $SERVER_LOG)"
[ "$HEALTH" = "ok" ] || fail "/health returned '$HEALTH', expected 'ok'"
echo "ok: /health -> ok"

# --- 3. /ohlcv byte-diff against baseline ------------------------------------
curl -sf "${BASE_URL}/ohlcv?${OHLCV_QUERY}" -o "$OUT_CSV" \
    || fail "/ohlcv request failed"
if ! diff -u "$BASELINE" "$OUT_CSV"; then
    fail "/ohlcv output differs from baseline $BASELINE"
fi
echo "ok: /ohlcv byte-identical to $BASELINE"

# --- 4. regression tests ------------------------------------------------------
"$PYTHON" -m pytest -m "not integration" -q || fail "regression tests are red"
echo "ok: regression tests green"

echo "VERIFY OK"
