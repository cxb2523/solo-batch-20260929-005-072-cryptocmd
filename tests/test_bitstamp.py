"""Unit tests for the Bitstamp exchange backend (mocked HTTP, no network)."""

from datetime import date, datetime, timedelta, timezone
import math
import os
from unittest.mock import MagicMock, patch

import pytest

import cryptocmd
from cryptocmd.exchanges import bitstamp
from cryptocmd.exchanges.bitstamp import (
    HEADERS,
    MAX_CANDLES_PER_REQUEST,
    _day_chunks,
    fetch_ohlcv,
    write_csv,
)

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


def _candle(day, close=100.0):
    ts = int(
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    )
    return {
        "timestamp": str(ts),
        "open": str(close - 1.0),
        "high": str(close + 1.0),
        "low": str(close - 2.0),
        "close": str(close),
        "volume": "10.0",
    }


def _response(candles, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.json.return_value = {"data": {"pair": "BTC/USD", "ohlc": candles}}
    resp.raise_for_status.return_value = None
    return resp


def _days(start, end):
    days = []
    day = start
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture(autouse=True)
def _no_fixture_env(monkeypatch):
    monkeypatch.delenv("CRYPTOCMD_BITSTAMP_FIXTURE", raising=False)


class TestSchemaAlignment:
    def test_headers_byte_aligned_with_cmc(self):
        assert HEADERS == CMC_HEADERS
        from cryptocmd import CmcScraper

        assert HEADERS == CmcScraper().headers

    def test_rows_match_cmc_shape(self):
        start, end = date(2024, 1, 1), date(2024, 1, 3)
        candles = [_candle(d, 100.0 + i) for i, d in enumerate(_days(start, end))]
        with patch("cryptocmd.exchanges.bitstamp.get") as mock_get:
            mock_get.return_value = _response(candles)
            headers, rows = fetch_ohlcv("btcusd", "01-01-2024", "03-01-2024")

        assert headers == CMC_HEADERS
        assert [r[0] for r in rows] == ["03-01-2024", "02-01-2024", "01-01-2024"]
        row = rows[-1]
        assert row[1:6] == [99.0, 101.0, 98.0, 100.0, 10.0]
        assert row[6] == ""  # Market Cap not provided by Bitstamp
        assert row[7] == "2024-01-01T00:00:00.000Z"
        assert row[10] == "2024-01-01T23:59:59.999Z"


class TestUtcDaySlicing:
    def test_chunks_never_exceed_request_cap(self):
        start = date(2020, 1, 1)
        end = date(2024, 12, 31)
        chunks = list(_day_chunks(start, end))
        assert all((e - s).days + 1 <= MAX_CANDLES_PER_REQUEST for s, e in chunks)
        # contiguous, gapless, covering the full range
        assert chunks[0][0] == start
        assert chunks[-1][1] == end
        for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
            assert next_start == prev_end + timedelta(days=1)

    def test_long_range_issues_multiple_requests(self):
        start = date(2020, 1, 1)
        end = date(2024, 12, 31)
        candles = [_candle(d) for d in _days(start, end)]
        with patch("cryptocmd.exchanges.bitstamp.get") as mock_get:
            mock_get.side_effect = lambda url, **kw: _response(
                [
                    c
                    for c in candles
                    if kw["params"]["start"]
                    <= int(c["timestamp"])
                    <= kw["params"]["end"]
                ]
            )
            _, rows = fetch_ohlcv("btcusd", "01-01-2020", "31-12-2024")
        assert mock_get.call_count == len(list(_day_chunks(start, end))) > 1
        assert len(rows) == (end - start).days + 1


class TestBackoff:
    def test_429_retries_with_exponential_backoff(self):
        ok = _response([_candle(date(2024, 1, 1))])
        throttled = _response([], status=429)
        with patch("cryptocmd.exchanges.bitstamp.get") as mock_get, patch(
            "cryptocmd.exchanges.bitstamp.time.sleep"
        ) as mock_sleep:
            mock_get.side_effect = [throttled, throttled, ok]
            fetch_ohlcv("btcusd", "01-01-2024", "01-01-2024")
        assert mock_get.call_count == 3
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays == sorted(delays)
        assert delays[1] > delays[0]

    def test_429_exhaustion_raises(self):
        with patch("cryptocmd.exchanges.bitstamp.get") as mock_get, patch(
            "cryptocmd.exchanges.bitstamp.time.sleep"
        ):
            throttled = _response([], status=429)
            throttled.raise_for_status.side_effect = Exception("429")
            mock_get.return_value = throttled
            with pytest.raises(Exception, match="429"):
                fetch_ohlcv("btcusd", "01-01-2024", "01-01-2024")


class TestIdempotency:
    def test_duplicate_candles_across_chunks_written_once(self, tmp_path):
        # overlapping responses (same candle in two chunks) must not duplicate
        day = date(2024, 1, 2)
        dup = _candle(day)
        candles = [_candle(d) for d in _days(date(2024, 1, 1), date(2024, 1, 3))]
        with patch("cryptocmd.exchanges.bitstamp.get") as mock_get:
            mock_get.return_value = _response(candles + [dup])
            headers, rows = fetch_ohlcv("btcusd", "01-01-2024", "03-01-2024")
        assert [r[0] for r in rows].count("02-01-2024") == 1

        out = tmp_path / "out.csv"
        write_csv(headers, rows + rows, str(out))  # writing twice the rows
        first = out.read_bytes()
        write_csv(headers, rows, str(out))  # and re-writing the file
        assert out.read_bytes() == first
        assert first.count(b"02-01-2024") == 1


class TestMissingDayPolicy:
    def test_missing_day_kept_as_nan_row(self):
        assert bitstamp.MISSING_POLICY == "nan"
        start, end = date(2024, 1, 1), date(2024, 1, 3)
        candles = [_candle(start), _candle(end)]  # 2024-01-02 missing
        with patch("cryptocmd.exchanges.bitstamp.get") as mock_get:
            mock_get.return_value = _response(candles)
            _, rows = fetch_ohlcv("btcusd", "01-01-2024", "03-01-2024")
        assert len(rows) == 3  # calendar kept complete, not forward-filled
        missing = next(r for r in rows if r[0] == "02-01-2024")
        assert all(math.isnan(v) for v in missing[1:6])

    def test_fixture_mode_matches_baseline(self, monkeypatch, tmp_path):
        fixture = os.path.join(
            os.path.dirname(__file__), "fixtures", "bitstamp_btcusd.json"
        )
        baseline = os.path.join(
            os.path.dirname(__file__),
            "baseline",
            "bitstamp_btcusd_01-01-2024_10-01-2024.csv",
        )
        monkeypatch.setenv("CRYPTOCMD_BITSTAMP_FIXTURE", fixture)
        headers, rows = fetch_ohlcv("btcusd", "01-01-2024", "10-01-2024")
        out = tmp_path / "out.csv"
        write_csv(headers, rows, str(out))
        with open(baseline, "rb") as fh:
            assert out.read_bytes() == fh.read()


class TestDispatch:
    def test_builtin_exchanges_registered(self):
        assert "bitstamp" in cryptocmd.available_exchanges()
        assert "cmc" in cryptocmd.available_exchanges()

    def test_third_party_registration_hook(self):
        class Dummy:
            def fetch_ohlcv(self, symbol, start_date=None, end_date=None, fiat="USD"):
                return HEADERS, []

        cryptocmd.register_exchange("dummy-test", Dummy())
        try:
            assert "dummy-test" in cryptocmd.available_exchanges()
            headers, _ = cryptocmd.fetch_ohlcv("dummy-test", "xyz")
            assert headers == HEADERS
        finally:
            cryptocmd.exchanges._BACKENDS.pop("dummy-test", None)

    def test_unknown_exchange_raises(self):
        with pytest.raises(KeyError):
            cryptocmd.get_exchange("no-such-exchange")
