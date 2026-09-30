"""Tests for the dispatch architecture, Bitstamp backend and local server."""

import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import cryptocmd
from cryptocmd import CmcScraper, get_backend, get_scraper, register_backend
from cryptocmd.exchanges import common
from cryptocmd.exchanges.bitstamp import (
    DAILY_STEP,
    BitstampScraper,
    FixtureTransport,
    candle_to_row,
)
from cryptocmd.exchanges.common import (
    DailyCandleStore,
    reindex_calendar,
    rows_to_csv_bytes,
    utc_day_chunks,
)

UTC = timezone.utc
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "cryptocmd", "exchanges", "fixtures")
BASELINE = os.path.join(REPO_ROOT, "tests", "fixtures", "bitstamp_btcusd_baseline.csv")

CMC_HEADERS = [
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


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("CRYPTOCMD_FIXTURE_DIR", FIXTURE_DIR)
    monkeypatch.setenv("CRYPTOCMD_CACHE_DIR", str(tmp_path / "cache"))
    yield tmp_path


# ---------------------------------------------------------------------------
# Dispatch / registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_builtin_backends_discovered(self):
        names = cryptocmd.available_backends()
        assert "bitstamp" in names
        assert "cmc" in names
        assert "coinmarketcap" in names

    def test_cmc_legacy_path_preserved(self):
        assert get_backend("cmc").scraper_class is CmcScraper
        assert get_backend("coinmarketcap").scraper_class is CmcScraper

    def test_third_party_register(self):
        dummy = type("DummyScraper", (), {})
        backend = cryptocmd.Backend("myx", dummy)
        register_backend("myx", backend)
        assert get_backend("myx").scraper_class is dummy

    def test_unknown_backend(self):
        with pytest.raises(cryptocmd.exchanges.UnknownBackend):
            get_backend("nope-not-an-exchange")

    def test_factory_picks_backend(self, offline):
        scraper = get_scraper("bitstamp", "btc", "01-01-2024", "02-01-2024")
        assert isinstance(scraper, BitstampScraper)


# ---------------------------------------------------------------------------
# UTC day-boundary slicing
# ---------------------------------------------------------------------------


class TestChunking:
    def test_single_request_under_limit(self):
        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 1, 5, tzinfo=UTC)
        chunks = list(utc_day_chunks(start, end, limit=1000))
        assert chunks == [(start, end)]

    def test_chunks_split_on_utc_days_without_gap_or_overlap(self):
        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 1, 10, tzinfo=UTC)
        chunks = list(utc_day_chunks(start, end, limit=3))
        # windows of 3,3,3,1 days, contiguous on day boundaries
        assert chunks == [
            (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC)),
            (datetime(2024, 1, 4, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC)),
            (datetime(2024, 1, 7, tzinfo=UTC), datetime(2024, 1, 9, tzinfo=UTC)),
            (datetime(2024, 1, 10, tzinfo=UTC), datetime(2024, 1, 10, tzinfo=UTC)),
        ]
        covered = []
        for chunk_start, chunk_end in chunks:
            cursor = chunk_start
            while cursor <= chunk_end:
                covered.append(cursor)
                cursor += timedelta(days=1)
        assert covered == [start + timedelta(days=i) for i in range(10)]


# ---------------------------------------------------------------------------
# 429 exponential backoff
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError("status {}".format(self.status_code))


class TestBackoff:
    def test_retries_on_429_with_exponential_delays(self):
        sleeps = []
        session = MagicMock()
        session.get.side_effect = [
            _FakeResponse(429, headers={"Retry-After": "0"}),
            _FakeResponse(429),
            _FakeResponse(200, {"data": {"ohlc": []}}),
        ]
        result = common.http_get_json(
            "https://example.test/x",
            params={"a": 1},
            session=session,
            max_retries=5,
            base_delay=1.0,
            sleep=sleeps.append,
        )
        assert result == {"data": {"ohlc": []}}
        assert session.get.call_count == 3
        assert sleeps == [1.0, 2.0]  # exponential doubling

    def test_honours_retry_after_header(self):
        sleeps = []
        session = MagicMock()
        session.get.side_effect = [
            _FakeResponse(429, headers={"Retry-After": "7"}),
            _FakeResponse(200, {"ok": True}),
        ]
        common.http_get_json(
            "https://example.test/x",
            session=session,
            base_delay=1.0,
            sleep=sleeps.append,
        )
        assert sleeps == [7.0]

    def test_gives_up_after_max_retries(self):
        session = MagicMock()
        session.get.return_value = _FakeResponse(429)
        with pytest.raises(Exception):
            common.http_get_json(
                "https://example.test/x",
                session=session,
                max_retries=2,
                base_delay=0.01,
                sleep=lambda _: None,
            )
        assert session.get.call_count == 3


# ---------------------------------------------------------------------------
# Idempotent on-disk candle store
# ---------------------------------------------------------------------------


class TestCandleStore:
    def test_put_then_load_is_idempotent(self, tmp_path):
        store = DailyCandleStore(str(tmp_path), "BTCUSD", DAILY_STEP)
        day = datetime(2024, 1, 1, tzinfo=UTC)
        candle = {"timestamp": str(int(day.timestamp())), "close": "100.0"}

        assert store.put(day, candle) is True
        # identical candle is not rewritten
        assert store.put(day, candle) is False
        # a different candle replaces it
        changed = dict(candle, close="101.0")
        assert store.put(day, changed) is True
        assert store.load(day)["close"] == "101.0"

    def test_repeated_fetch_does_not_duplicate(self, offline):
        scraper = BitstampScraper(
            "btc",
            "15-12-2023",
            "20-12-2023",
            cache_dir=str(offline / "cache"),
            transport=FixtureTransport(FIXTURE_DIR),
        )
        scraper.get_data()
        files_before = set(os.listdir(scraper.store.dir))

        again = BitstampScraper(
            "btc",
            "15-12-2023",
            "20-12-2023",
            cache_dir=str(offline / "cache"),
            transport=FixtureTransport(FIXTURE_DIR),
        )
        again.get_data()
        files_after = set(os.listdir(again.store.dir))
        assert files_before == files_after
        # exactly one file per UTC day, no duplicates
        assert len(files_after) == 6


# ---------------------------------------------------------------------------
# Missing-day policy: NaN retained vs forward fill
# ---------------------------------------------------------------------------


class TestMissingPolicy:
    def _row(self, day, close):
        return [
            day.strftime("%d-%m-%Y"),
            1,
            2,
            0.5,
            close,
            3,
            None,
            None,
            None,
            None,
            None,
        ]

    def test_nan_policy_keeps_missing_row(self, monkeypatch):
        monkeypatch.setenv("CRYPTOCMD_MISSING_POLICY", "nan")
        d0 = datetime(2024, 1, 1, tzinfo=UTC)
        d1 = datetime(2024, 1, 2, tzinfo=UTC)
        candles = {d0: self._row(d0, 100.0)}
        aligned = reindex_calendar(candles, d0, d1, "nan")
        assert aligned[d1][4] is None

    def test_ffill_policy_forward_fills(self, monkeypatch):
        monkeypatch.setenv("CRYPTOCMD_MISSING_POLICY", "ffill")
        d0 = datetime(2024, 1, 1, tzinfo=UTC)
        d1 = datetime(2024, 1, 2, tzinfo=UTC)
        d2 = datetime(2024, 1, 3, tzinfo=UTC)
        candles = {d0: self._row(d0, 100.0)}
        aligned = reindex_calendar(candles, d0, d2, "ffill")
        assert aligned[d1][4] == 100.0
        assert aligned[d2][4] == 100.0
        assert aligned[d1][0] == "02-01-2024"

    def test_invalid_policy_rejected(self, monkeypatch):
        monkeypatch.setenv("CRYPTOCMD_MISSING_POLICY", "interpolate")
        with pytest.raises(common.InvalidPolicy):
            common.get_missing_policy()


# ---------------------------------------------------------------------------
# Bitstamp output shape + CMC byte alignment
# ---------------------------------------------------------------------------


class TestBitstampOutput:
    def _scraper(self, cache, **kwargs):
        defaults = dict(
            coin_code="btc",
            start_date="15-12-2023",
            end_date="15-01-2024",
            fiat="usd",
            cache_dir=str(cache / "cache"),
            transport=FixtureTransport(FIXTURE_DIR),
        )
        defaults.update(kwargs)
        return BitstampScraper(**defaults)

    def test_headers_align_with_cmc(self, offline):
        scraper = self._scraper(offline)
        headers, _ = scraper.get_data()
        assert headers == CMC_HEADERS

    def test_full_calendar_and_three_missing_days(self, offline):
        scraper = self._scraper(offline)
        _, rows = scraper.get_data()
        assert len(rows) == 32  # 15 Dec 2023 .. 15 Jan 2024 inclusive
        missing = {r[0] for r in rows if r[4] is None}
        assert missing == {"06-01-2024", "07-01-2024", "08-01-2024"}
        # newest first, like the CMC default
        assert rows[0][0] == "15-01-2024"
        assert rows[-1][0] == "15-12-2023"

    def test_dataframe_index_and_columns_match_cmc(self, offline):
        scraper = self._scraper(offline)
        df = scraper.get_dataframe(date_as_index=True)
        assert df.index.name == "Date"
        assert list(df.columns) == CMC_HEADERS[1:]
        assert len(df) == 32

    def test_csv_is_byte_identical_to_baseline(self, offline):
        scraper = self._scraper(offline)
        with open(BASELINE, "rb") as handle:
            assert scraper.to_csv_bytes() == handle.read()

    def test_server_csv_writer_matches_tablib(self, offline):
        # the /ohlcv handler uses a default csv.writer; it must serialize the
        # same rows identically to the tablib export path used by examples
        scraper = self._scraper(offline)
        headers, rows = scraper.get_data()
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(headers)
        writer.writerows(rows)
        assert buffer.getvalue().encode("utf-8") == scraper.to_csv_bytes()

    def test_candle_mapping_layout(self):
        day = datetime(2024, 1, 1, tzinfo=UTC)
        candle = {
            "timestamp": str(int(day.timestamp())),
            "open": "1",
            "high": "2",
            "low": "0.5",
            "close": "1.5",
            "volume": "10",
        }
        row = candle_to_row(candle, day)
        assert row[0] == "01-01-2024"
        assert row[1:6] == [1.0, 2.0, 0.5, 1.5, 10.0]
        assert row[6] is None  # Market Cap not provided
        assert row[7] == "2024-01-01T00:00:00.000Z"
        assert row[10] == "2024-01-01T23:59:59.999Z"

    def test_ffill_rendered_via_scraper(self, offline, monkeypatch):
        monkeypatch.setenv("CRYPTOCMD_MISSING_POLICY", "ffill")
        scraper = self._scraper(offline)
        _, rows = scraper.get_data()
        by_date = {r[0]: r for r in rows}
        # 06-08 Jan forward-filled from the 05 Jan close
        assert by_date["06-01-2024"][4] == by_date["05-01-2024"][4]
        assert by_date["08-01-2024"][4] == by_date["05-01-2024"][4]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class TestServer:
    @pytest.fixture
    def client(self, offline):
        from cryptocmd import server
        from http.client import HTTPConnection

        context = server.ServiceContext(
            "bitstamp",
            "btc",
            "usd",
            fixture_dir=FIXTURE_DIR,
            cache_dir=str(offline / "cache"),
        )
        httpd = server.ThreadingHTTPServer(
            ("127.0.0.1", 0), server.make_handler(context)
        )
        import threading

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]

        def get(path):
            conn = HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read(), dict(resp.getheaders())

        yield get
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    def test_health(self, client):
        status, body, _ = client("/health")
        assert status == 200
        assert body == b"ok"

    def test_dashboard_served(self, client):
        status, body, _ = client("/")
        assert status == 200
        assert b"missing points" in body
        assert b'data-range="7d"' in body and b'data-range="all"' in body

    def test_ohlcv_byte_identical_to_baseline(self, client):
        status, body, headers = client(
            "/ohlcv?exchange=bitstamp&coin=btc&fiat=usd&start=15-12-2023&end=15-01-2024"
        )
        assert status == 200
        with open(BASELINE, "rb") as handle:
            assert body == handle.read()
        assert headers.get("X-Missing-Count") == "3"

    def test_ohlcv_json_missing_count(self, client):
        status, body, _ = client(
            "/ohlcv?exchange=bitstamp&coin=btc&fiat=usd&range=30d&format=json"
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["missing"] == 3
        assert payload["count"] == 30


class _MultiChunkTransport:
    """Fake transport that records requests and returns 3 candles per page."""

    def __init__(self):
        self.calls = []

    def get_ohlc(self, pair, step, start_dt, limit):
        self.calls.append((start_dt, limit))
        out = []
        for offset in range(limit):
            day = start_dt + timedelta(days=offset)
            if day <= datetime(2024, 1, 7, tzinfo=UTC):
                ts = str(int(day.timestamp()))
                out.append({"timestamp": ts, "open": "1", "high": "2",
                            "low": "0.5", "close": "1.5", "volume": "9"})
        return out

    def bounds(self, pair, step):
        return (datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2024, 1, 7, tzinfo=UTC))


class TestMultiChunkFetch:
    def test_large_range_sliced_and_assembled(self, offline, monkeypatch):
        # force a 3-day request cap so 7 calendar days span 3 HTTP requests
        monkeypatch.setattr("cryptocmd.exchanges.bitstamp.DEFAULT_REQUEST_LIMIT", 3)
        transport = _MultiChunkTransport()
        scraper = BitstampScraper(
            "btc", "01-01-2024", "07-01-2024",
            cache_dir=str(offline / "mc-cache"), transport=transport,
        )
        _, rows = scraper.get_data()
        dates = [r[0] for r in rows]
        assert len(rows) == 7
        assert dates == [
            "07-01-2024", "06-01-2024", "05-01-2024", "04-01-2024",
            "03-01-2024", "02-01-2024", "01-01-2024",
        ]
        requested = [(start, limit) for start, limit in transport.calls]
        assert requested == [
            (datetime(2024, 1, 1, tzinfo=UTC), 3),
            (datetime(2024, 1, 4, tzinfo=UTC), 3),
            (datetime(2024, 1, 7, tzinfo=UTC), 1),
        ]
