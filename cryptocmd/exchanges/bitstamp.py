"""Bitstamp OHLCV backend.

Pulls daily OHLCV candles from Bitstamp's public API and reshapes them into
the exact CMC row schema so downstream consumers (``examples/get_csv.py``,
existing exports, the local server) stay byte-compatible.

Hard constraints honoured here:

* Output columns and index are byte-aligned with the CMC schema
  (``HEADERS`` is identical to ``CmcScraper.headers``).
* Ranges wider than a single request's candle cap are sliced on UTC day
  boundaries; HTTP 429 responses are retried with exponential backoff;
  candles are keyed by UTC day so merging chunks (and re-writing the CSV)
  is idempotent — the same candle is never written twice.
* Missing days / trading halts: the single pipeline-wide policy is
  ``MISSING_POLICY = "nan"`` — the UTC daily calendar is kept complete and
  missing days are emitted as NaN rows (never forward-filled).
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
import json
import math
import os
import tempfile
import time

from requests import get

name = "bitstamp"

# Byte-identical to CmcScraper.headers — do not reorder or rename.
HEADERS = [
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "Market Cap",
    "Time Open",
    "Time High",
    "Time Low",
    "Time Close",
]

# Pipeline-wide missing-data policy: keep NaN rows (no forward-fill anywhere).
MISSING_POLICY = "nan"

STEP_SECONDS = 86400  # daily candles
MAX_CANDLES_PER_REQUEST = 1000  # Bitstamp ohlc cap per request
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0

_DEFAULT_SYMBOL = "btcusd"
_DEFAULT_RANGE_DAYS = 30

_HEADERS_UA = {"User-Agent": "Mozilla/5.0"}


def _api_base():
    return os.environ.get("CRYPTOCMD_BITSTAMP_BASE_URL", "https://www.bitstamp.net/api/v2")


def _parse_date(value):
    """Parse 'dd-mm-yyyy' (CMC convention) or 'yyyy-mm-dd' into a UTC date."""
    if isinstance(value, date):
        return value
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc).date()
        except ValueError:
            continue
    raise ValueError(
        f"Unrecognised date {value!r}; expected dd-mm-yyyy or yyyy-mm-dd"
    )


def _day_chunks(start_day, end_day):
    """Yield (chunk_start, chunk_end) date slices of at most
    MAX_CANDLES_PER_REQUEST days, aligned to UTC day boundaries."""
    chunk_start = start_day
    while chunk_start <= end_day:
        chunk_end = min(chunk_start + timedelta(days=MAX_CANDLES_PER_REQUEST - 1), end_day)
        yield chunk_start, chunk_end
        chunk_start = chunk_end + timedelta(days=1)


def _get_with_backoff(url, params):
    """GET *url*; on HTTP 429 retry with exponential backoff."""
    for attempt in range(MAX_RETRIES + 1):
        response = get(url, params=params, headers=_HEADERS_UA, timeout=30)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        if attempt == MAX_RETRIES:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
        else:
            delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
        time.sleep(min(delay, BACKOFF_MAX_SECONDS))
    raise RuntimeError("unreachable")  # pragma: no cover


def _day_start_ts(day):
    return int(datetime.combine(day, dtime.min, tzinfo=timezone.utc).timestamp())


def _fetch_candles_remote(pair, start_day, end_day):
    """Fetch daily candles for [start_day, end_day] from Bitstamp, slicing
    the range on UTC day boundaries so no request exceeds the candle cap."""
    candles = {}
    for chunk_start, chunk_end in _day_chunks(start_day, end_day):
        n_days = (chunk_end - chunk_start).days + 1
        response = _get_with_backoff(
            f"{_api_base()}/ohlc/{pair}/",
            params={
                "step": STEP_SECONDS,
                "limit": min(n_days, MAX_CANDLES_PER_REQUEST),
                "start": _day_start_ts(chunk_start),
                "end": _day_start_ts(chunk_end) + STEP_SECONDS - 1,
            },
        )
        payload = response.json()
        for candle in payload.get("data", {}).get("ohlc", []):
            ts = int(candle["timestamp"])
            day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            if start_day <= day <= end_day:
                candles[day] = candle  # keyed by day: re-fetching is idempotent
    return candles


def _fetch_candles_fixture(pair, start_day, end_day):
    """Offline mode: serve candles from a local Bitstamp-shaped JSON fixture
    (env CRYPTOCMD_BITSTAMP_FIXTURE). Used by scripts/verify.sh so the byte
    diff against the baseline CSV is hermetic."""
    fixture_path = os.environ["CRYPTOCMD_BITSTAMP_FIXTURE"]
    with open(fixture_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    candles = {}
    for candle in payload.get("data", {}).get("ohlc", []):
        ts = int(candle["timestamp"])
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if start_day <= day <= end_day:
            candles[day] = candle
    return candles


def fetch_candles(symbol=None, start_date=None, end_date=None):
    """Return {date: candle} for the requested range (ascending dict)."""
    pair = (symbol or _DEFAULT_SYMBOL).strip().lower()
    end_day = _parse_date(end_date) if end_date else datetime.now(timezone.utc).date()
    start_day = (
        _parse_date(start_date)
        if start_date
        else end_day - timedelta(days=_DEFAULT_RANGE_DAYS - 1)
    )
    if start_day > end_day:
        raise ValueError(f"start_date {start_day} is after end_date {end_day}")

    if os.environ.get("CRYPTOCMD_BITSTAMP_FIXTURE"):
        candles = _fetch_candles_fixture(pair, start_day, end_day)
    else:
        candles = _fetch_candles_remote(pair, start_day, end_day)

    # Reindex onto the complete UTC daily calendar: missing days / halts
    # stay as NaN rows (MISSING_POLICY = "nan"), never forward-filled.
    full = {}
    day = start_day
    while day <= end_day:
        full[day] = candles.get(day)
        day += timedelta(days=1)
    return full


def _iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _candle_to_row(day, candle):
    date_str = day.strftime("%d-%m-%Y")
    if candle is None:
        # Missing day / halt: keep NaN for numeric fields (uniform policy).
        nan = math.nan
        return [date_str, nan, nan, nan, nan, nan, "", "", "", "", ""]
    day_open = datetime.combine(day, dtime.min, tzinfo=timezone.utc)
    day_close = day_open + timedelta(seconds=STEP_SECONDS - 1)
    return [
        date_str,
        float(candle["open"]),
        float(candle["high"]),
        float(candle["low"]),
        float(candle["close"]),
        float(candle["volume"]),
        "",  # Market Cap: not provided by Bitstamp
        _iso_z(day_open),
        "",  # Time High: not provided by Bitstamp
        "",  # Time Low: not provided by Bitstamp
        _iso_z(day_close.replace(microsecond=999000)),
    ]


def fetch_ohlcv(symbol=None, start_date=None, end_date=None, fiat="USD"):
    """Fetch OHLCV rows in the CMC schema.

    Returns ``(headers, rows)`` with rows ordered newest-first, matching the
    default CmcScraper output byte-for-byte in structure.
    """
    candles = fetch_candles(symbol, start_date, end_date)
    rows = [_candle_to_row(day, candles[day]) for day in sorted(candles, reverse=True)]
    return list(HEADERS), rows


def render_csv(headers, rows):
    """Serialise to CSV bytes identical to CmcScraper.export_csv output."""
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=",", quoting=csv.QUOTE_NONNUMERIC)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def write_csv(headers, rows, path):
    """Idempotently persist rows to *path*.

    Rows are de-duplicated by Date (first occurrence wins) and the file is
    written atomically (temp file + os.replace), so re-running never writes
    the same candle twice and never leaves a partial file.
    """
    seen = set()
    unique_rows = []
    for row in rows:
        if row[0] in seen:
            continue
        seen.add(row[0])
        unique_rows.append(row)

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            fh.write(render_csv(headers, unique_rows))
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path
