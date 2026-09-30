"""Bitstamp OHLCV backend.

Adds an exchange without touching package internals: this module exposes a
module-level :data:`BACKEND` which ``cryptocmd.exchanges.registry`` discovers
automatically.

Data comes from the public v2 endpoint
``https://www.bitstamp.net/api/v2/ohlc/{pair}/`` (daily candles, ``step=86400``).
Ranges wider than a single request are sliced on UTC day boundaries, HTTP 429
responses trigger exponential backoff, and every candle is written through an
idempotent per-day store so repeated fetches never duplicate a candle.
"""

from __future__ import annotations

from datetime import timedelta
import json
import os

from ..utils import InvalidParameters  # noqa: F401  (re-exported for callers)
from .backend import Backend
from .base import ExchangeScraper
from .common import (
    DEFAULT_FIXTURE_DIR,
    DEFAULT_REQUEST_LIMIT,
    UTC,
    DailyCandleStore,
    get_missing_policy,
    http_get_json,
    parse_ddmmyyyy,
    reindex_calendar,
    utc_day_chunks,
)

BITSTAMP_API = "https://www.bitstamp.net/api/v2"
DAILY_STEP = 86400


class BitstampError(RuntimeError):
    """The Bitstamp API returned no usable OHLCV payload."""


class HttpTransport:
    """Live transport; all HTTP goes through :func:`http_get_json` (429-aware)."""

    def __init__(self, session=None, max_retries=5, base_delay=1.0, sleep=None):
        self.session = session
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep = sleep

    def _get_json(self, url, params):
        kwargs = {}
        if self.sleep is not None:
            kwargs["sleep"] = self.sleep
        return http_get_json(
            url,
            params=params,
            session=self.session,
            max_retries=self.max_retries,
            base_delay=self.base_delay,
            **kwargs,
        )

    def get_ohlc(self, pair, step, start_dt, limit):
        url = f"{BITSTAMP_API}/ohlc/{pair.lower()}/"
        params = {"step": step, "limit": limit, "start": int(start_dt.timestamp())}
        payload = self._get_json(url, params)
        return payload.get("data", {}).get("ohlc", []) or []

    def bounds(self, pair, step):
        url = f"{BITSTAMP_API}/ohlc/{pair.lower()}/"
        first = (
            self._get_json(url, {"step": step, "limit": 1, "start": 0})
            .get("data", {})
            .get("ohlc", [])
            or []
        )
        latest = (
            self._get_json(url, {"step": step, "limit": 1})
            .get("data", {})
            .get("ohlc", [])
            or []
        )
        if not first or not latest:
            raise BitstampError(f"no candles returned for pair '{pair}'")
        return _to_day(first[0]["timestamp"]), _to_day(latest[0]["timestamp"])


class FixtureTransport(HttpTransport):
    """Offline transport replaying captured responses, with identical filtering."""

    def __init__(self, fixture_dir=DEFAULT_FIXTURE_DIR, **kwargs):
        super().__init__(**kwargs)
        self.fixture_dir = fixture_dir
        self._cache = {}

    def _load(self, pair, step):
        key = (pair.lower(), step)
        if key not in self._cache:
            path = os.path.join(self.fixture_dir, "{}_{}.json".format(*key))
            with open(path, "r", encoding="utf-8") as handle:
                self._cache[key] = json.load(handle)
        return self._cache[key]

    def _get_json(self, url, params):
        pair = url.rstrip("/").split("/")[-1].upper()
        step = int(params["step"])
        start_ts = int(params.get("start", 0) or 0)
        limit = int(params["limit"])
        candles = [
            candle
            for candle in self._load(pair, step)
            if int(candle["timestamp"]) >= start_ts
        ][:limit]
        return {"data": {"pair": pair, "ohlc": candles}}

    def bounds(self, pair, step):
        candles = self._load(pair, step)
        return _to_day(candles[0]["timestamp"]), _to_day(candles[-1]["timestamp"])


def _to_day(timestamp):
    from datetime import datetime

    return datetime.fromtimestamp(int(timestamp), tz=UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def get_transport():
    """Pick the offline fixture transport when ``CRYPTOCMD_FIXTURE_DIR`` is set."""
    fixture_dir = os.environ.get("CRYPTOCMD_FIXTURE_DIR")
    if fixture_dir:
        return FixtureTransport(fixture_dir=fixture_dir)
    return HttpTransport()


def candle_to_row(candle, day):
    """Map a Bitstamp daily candle to the CMC column layout."""
    return [
        day.strftime("%d-%m-%Y"),
        float(candle["open"]),
        float(candle["high"]),
        float(candle["low"]),
        float(candle["close"]),
        float(candle["volume"]),
        None,  # Market Cap is not published by Bitstamp
        day.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        None,  # intraday high timestamp is not published
        None,  # intraday low timestamp is not published
        day.strftime("%Y-%m-%dT23:59:59.999Z"),
    ]


class BitstampScraper(ExchangeScraper):
    """Fetch daily OHLCV from Bitstamp with CMC-identical output columns/index."""

    exchange = "bitstamp"

    def __init__(
        self,
        coin_code=None,
        start_date=None,
        end_date=None,
        all_time=False,
        order_ascending=False,
        fiat="USD",
        coin_name=None,
        id_number=None,
        cache_dir=None,
        transport=None,
    ):
        super().__init__(
            coin_code=coin_code,
            start_date=start_date,
            end_date=end_date,
            all_time=all_time,
            order_ascending=order_ascending,
            fiat=fiat,
            coin_name=coin_name,
            id_number=id_number,
            cache_dir=cache_dir,
        )
        self.pair = "{}{}".format(
            (coin_code or "").strip().upper(), (fiat or "").strip().upper()
        )
        self.transport = transport or get_transport()
        cache_root = cache_dir or os.environ.get(
            "CRYPTOCMD_CACHE_DIR", os.path.join(os.path.expanduser("~"), ".cryptocmd")
        )
        self.store = DailyCandleStore(cache_root, self.pair, DAILY_STEP)

    def __repr__(self):
        return f"<BitstampScraper pair:{self.pair}, start_date:{self.start_date}, end_date:{self.end_date}, all_time:{self.all_time}>"

    def _window(self):
        earliest, latest = self.transport.bounds(self.pair, DAILY_STEP)
        if self.all_time:
            return earliest, latest
        requested_start = parse_ddmmyyyy(self.start_date)
        requested_end = parse_ddmmyyyy(self.end_date)
        start = max(requested_start, earliest)
        end = min(requested_end, latest)
        if start > end:
            return start, start - timedelta(days=1)
        return start, end

    def _fetch_rows(self):
        start_day, end_day = self._window()
        if end_day < start_day:
            return []

        by_day = self.store.load_all()
        for chunk_start, chunk_end in utc_day_chunks(
            start_day, end_day, DEFAULT_REQUEST_LIMIT
        ):
            limit = (chunk_end - chunk_start).days + 1
            for candle in self.transport.get_ohlc(
                self.pair, DAILY_STEP, chunk_start, limit
            ):
                day = _to_day(candle["timestamp"])
                if start_day <= day <= end_day:
                    by_day[day] = candle
                    self.store.put(day, candle)

        row_by_day = {day: candle_to_row(candle, day) for day, candle in by_day.items()}
        policy = get_missing_policy()
        aligned = reindex_calendar(row_by_day, start_day, end_day, policy)
        return [aligned[day] for day in sorted(aligned, reverse=True)]


BACKEND = Backend("bitstamp", BitstampScraper, aliases=("bitstamp.net",))
