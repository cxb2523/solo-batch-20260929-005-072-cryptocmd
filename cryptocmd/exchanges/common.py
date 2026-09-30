"""Common building blocks shared by exchange backends."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import io
import os
import time

import requests

UTC = timezone.utc

# Missing-day / trading-halt policy, enforced on the whole pipeline.
# "nan"   -> reindex the full UTC calendar and keep missing days as NaN rows.
# "ffill" -> forward-fill the most recent observed candle.
# Only these two values are legal; every backend reads this constant.
MISSING_POLICY = os.environ.get("CRYPTOCMD_MISSING_POLICY", "nan").lower()

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

# Max daily candles a single HTTP request may span. Bitstamp caps at 1000.
DEFAULT_REQUEST_LIMIT = 1000

DEFAULT_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


class InvalidPolicy(ValueError):
    """The configured missing-data policy is neither ``nan`` nor ``ffill``."""


def get_missing_policy():
    """Return the validated, pipeline-wide missing-data policy."""
    policy = os.environ.get("CRYPTOCMD_MISSING_POLICY", MISSING_POLICY).lower()
    if policy not in ("nan", "ffill"):
        raise InvalidPolicy(
            f"CRYPTOCMD_MISSING_POLICY must be 'nan' or 'ffill', got '{policy}'"
        )
    return policy


def parse_ddmmyyyy(value):
    """Parse a ``dd-mm-yyyy`` date string into a timezone-aware UTC datetime."""
    return datetime.strptime(value, "%d-%m-%Y").replace(tzinfo=UTC)


def utc_day_chunks(start_dt, end_dt, limit=DEFAULT_REQUEST_LIMIT):
    """Yield ``(chunk_start, chunk_end)`` UTC-day windows of at most *limit* days.

    Windows are split strictly on UTC day boundaries and are contiguous: the
    next chunk starts the day after the previous one ended, so no candle is
    requested twice or skipped.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    cursor = start_dt
    while cursor <= end_dt:
        chunk_end = min(cursor + timedelta(days=limit - 1), end_dt)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def http_get_json(
    url,
    params=None,
    session=None,
    max_retries=5,
    base_delay=1.0,
    sleep=time.sleep,
    headers=None,
):
    """GET JSON with exponential backoff on HTTP 429.

    Retry delay doubles on every 429 (``base_delay``, 2x, 4x ...), honouring a
    server ``Retry-After`` header when present. ``sleep`` is injectable so the
    backoff is deterministic in tests.
    """
    http = session or requests
    request_headers = {"User-Agent": "Mozilla/5.0"}
    if headers:
        request_headers.update(headers)

    attempt = 0
    while True:
        response = http.get(url, params=params, headers=request_headers)
        if response.status_code != 429:
            response.raise_for_status()
            return response.json()
        if attempt >= max_retries:
            response.raise_for_status()
        retry_after = 0.0
        try:
            retry_after = float(response.headers.get("Retry-After", 0) or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        delay = max(retry_after, base_delay * (2**attempt))
        sleep(delay)
        attempt += 1


def rows_to_csv_bytes(headers, rows):
    """Serialise rows to CSV bytes using the exact CMC dialect.

    Mirrors ``csv.writer(delimiter=",", quoting=csv.QUOTE_NONNUMERIC)`` from
    :meth:`cryptocmd.core.CmcScraper.export_csv`, so exchange output is
    byte-for-byte aligned with the CMC path.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=",", quoting=csv.QUOTE_NONNUMERIC)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def reindex_calendar(candles, start_day, end_day, policy=None):
    """Align *candles* (day -> row) onto every UTC day of the requested window.

    Missing days are either kept as NaN rows (``policy="nan"``) or forward
    filled from the previous observation (``policy="ffill"``). The returned
    mapping always covers the contiguous day range ``start_day..end_day``.
    """
    policy = policy or get_missing_policy()
    if policy not in ("nan", "ffill"):
        raise InvalidPolicy("policy must be 'nan' or 'ffill'")

    aligned = {}
    last_observed = None
    cursor = start_day
    while cursor <= end_day:
        row = candles.get(cursor)
        if row is not None:
            last_observed = row
            aligned[cursor] = row
        elif policy == "ffill" and last_observed is not None:
            filled = [cursor.strftime("%d-%m-%Y")] + list(last_observed[1:])
            aligned[cursor] = filled
        else:
            aligned[cursor] = [cursor.strftime("%d-%m-%Y")] + [None] * (
                len(HEADERS) - 1
            )
        cursor += timedelta(days=1)
    return aligned


class DailyCandleStore:
    """Idempotent on-disk store of one daily candle per file.

    Each UTC trading day is a single JSON file, written atomically and merged
    by candle key, so re-fetching the same candle never duplicates it on disk.
    """

    def __init__(self, root, pair, step=86400):
        self.dir = os.path.join(root, pair.upper(), str(step))
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, day):
        return os.path.join(self.dir, f"{day.date().isoformat()}.json")

    def has(self, day):
        return os.path.exists(self._path(day))

    def load(self, day):
        import json

        with open(self._path(day), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def put(self, day, candle):
        """Persist one candle for *day*. No-op when the identical candle exists."""
        import json

        path = self._path(day)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                if handle.read() == json.dumps(candle, sort_keys=True):
                    return False
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(candle, handle, sort_keys=True)
        os.replace(tmp_path, path)
        return True

    def load_all(self):
        """Return ``{day: candle}`` for every candle currently stored."""
        import json

        candles = {}
        for name in os.listdir(self.dir):
            if not name.endswith(".json"):
                continue
            try:
                day = datetime.strptime(name[:-5], "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            with open(os.path.join(self.dir, name), "r", encoding="utf-8") as handle:
                candles[day] = json.load(handle)
        return candles
