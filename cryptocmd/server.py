"""Local OHLCV service.

Run with::

    python -m cryptocmd.server --fixtures          # offline, deterministic
    python -m cryptocmd.server --exchange bitstamp # live data

Endpoints:

* ``GET /health`` -> ``ok``
* ``GET /ohlcv``  -> CSV (default) or ``format=json``. Query params:
  ``exchange``, ``coin``, ``fiat``, ``start``, ``end`` (dd-mm-yyyy),
  ``range`` in {7d,30d,90d,all}.
* ``GET /``       -> dashboard with range switching and missing-point count.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import tempfile
from urllib.parse import parse_qs, urlparse

from . import DEFAULT_EXCHANGE, get_scraper
from .exchanges.common import DEFAULT_FIXTURE_DIR, UTC

DEFAULT_PORT = 8042
PRESET_DAYS = {"7d": 7, "30d": 30, "90d": 90}


def _fixture_anchor(fixture_dir):
    """Return the last fixture candle day as the deterministic 'today' anchor."""
    latest = None
    for name in os.listdir(fixture_dir):
        if not name.endswith("_86400.json"):
            continue
        path = os.path.join(fixture_dir, name)
        with open(path, "r", encoding="utf-8") as handle:
            candles = json.load(handle)
        if candles:
            day = datetime.fromtimestamp(int(candles[-1]["timestamp"]), tz=UTC)
            latest = day if latest is None else max(latest, day)
    if latest is None:
        raise RuntimeError(f"no fixture candles found in {fixture_dir}")
    return latest


class ServiceContext:
    def __init__(self, exchange, coin, fiat, fixture_dir=None, cache_dir=None):
        self.exchange = exchange
        self.coin = coin
        self.fiat = fiat
        self.fixture_dir = fixture_dir
        self.cache_dir = cache_dir
        if fixture_dir:
            os.environ["CRYPTOCMD_FIXTURE_DIR"] = fixture_dir
            self.anchor = _fixture_anchor(fixture_dir)
        else:
            self.anchor = datetime.now(tz=UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

    def resolve_range(self, params):
        end_day = self.anchor
        start_day = None
        range_key = (params.get("range", ["30d"])[0] or "30d").lower()
        start = params.get("start", [None])[0]
        end = params.get("end", [None])[0]
        if start and end:
            start_day = datetime.strptime(start, "%d-%m-%Y").replace(tzinfo=UTC)
            end_day = datetime.strptime(end, "%d-%m-%Y").replace(tzinfo=UTC)
        elif range_key in PRESET_DAYS:
            start_day = end_day - timedelta(days=PRESET_DAYS[range_key] - 1)
        return start_day, end_day, range_key

    def build_scraper(self, params):
        exchange = params.get("exchange", [self.exchange])[0]
        coin = params.get("coin", [self.coin])[0]
        fiat = params.get("fiat", [self.fiat])[0]
        start_day, end_day, range_key = self.resolve_range(params)
        if range_key == "all" or start_day is None:
            scraper = get_scraper(
                exchange, coin, all_time=True, fiat=fiat, cache_dir=self.cache_dir
            )
        else:
            scraper = get_scraper(
                exchange,
                coin,
                start_day.strftime("%d-%m-%Y"),
                end_day.strftime("%d-%m-%Y"),
                fiat=fiat,
                cache_dir=self.cache_dir,
            )
        return scraper


def _rows_to_json(headers, rows):
    records = []
    missing = 0
    for row in rows:
        record = dict(zip(headers, row))
        if record.get("Close") is None:
            missing += 1
        records.append(record)
    return {"missing": missing, "count": len(rows), "records": records}


def _missing_count(rows):
    return sum(1 for row in rows if row[4] is None)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>cryptocmd &middot; OHLCV</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: ui-sans-serif, system-ui, Segoe UI, Roboto, sans-serif;
         margin: 0; padding: 24px; background:#0b0e14; color:#e6e9ef; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color:#8b93a7; font-size: 13px; margin-bottom: 16px; }
  .controls { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
  button { background:#1b2230; color:#e6e9ef; border:1px solid #2c3547;
           border-radius:8px; padding:7px 14px; font-size:13px; cursor:pointer; }
  button.active { background:#2f6df0; border-color:#2f6df0; }
  .stats { display:flex; gap:24px; margin-bottom:12px; font-size:14px; }
  .stats b { font-size:20px; display:block; }
  .miss { color:#ff8a8a; }
  svg { width:100%; height:280px; background:#0e131d; border:1px solid #20283a;
        border-radius:10px; }
  table { border-collapse:collapse; margin-top:16px; font-size:12px;
          width:100%; max-width:760px; }
  th, td { border-bottom:1px solid #20283a; padding:5px 8px; text-align:right; }
  th:first-child, td:first-child { text-align:left; }
  tr.miss td { color:#ff8a8a; }
  .dot { fill:#ff8a8a; }
</style>
</head>
<body>
  <h1>cryptocmd &mdash; <span id="pair"></span> daily OHLCV</h1>
  <div class="sub">UTC daily candles &middot; missing/halted days kept as NaN
       (<span id="policy"></span>) &middot; source <span id="source"></span></div>
  <div class="controls" id="ranges">
    <button data-range="7d">7D</button>
    <button data-range="30d" class="active">30D</button>
    <button data-range="90d">90D</button>
    <button data-range="all">ALL</button>
  </div>
  <div class="stats">
    <div>candles<b id="count">–</b></div>
    <div>missing points<b class="miss" id="missing">–</b></div>
    <div>window<b id="window" style="font-size:14px">–</b></div>
  </div>
  <svg id="chart" viewBox="0 0 760 280" preserveAspectRatio="none"></svg>
  <table id="table">
    <thead><tr>
      <th>Date</th><th>Open</th><th>High</th><th>Low</th>
      <th>Close</th><th>Volume</th>
    </tr></thead>
    <tbody></tbody>
  </table>

<script>
const qs = new URLSearchParams(location.search);
const coin = qs.get("coin") || "btc";
const fiat = qs.get("fiat") || "usd";
const exchange = qs.get("exchange") || "bitstamp";
document.getElementById("pair").textContent = (coin + "/" + fiat).toUpperCase();
document.getElementById("source").textContent = exchange;
document.getElementById("policy").textContent = "NaN retained";

async function load(range) {
  const url = "/ohlcv?exchange=" + exchange + "&coin=" + coin +
              "&fiat=" + fiat + "&range=" + range + "&format=json";
  const res = await fetch(url);
  const data = await res.json();
  render(range, data);
}

function render(range, data) {
  document.querySelectorAll("#ranges button").forEach(b =>
    b.classList.toggle("active", b.dataset.range === range));
  const recs = data.records;
  document.getElementById("count").textContent = data.count;
  document.getElementById("missing").textContent = data.missing;
  document.getElementById("window").textContent =
    recs.length ? (recs[recs.length - 1].Date + " → " + recs[0].Date) : "–";

  // chart
  const svg = document.getElementById("chart");
  svg.innerHTML = "";
  const W = 760, H = 280, pad = 24;
  const closes = recs.map(r => r.Close).filter(v => v !== null);
  let min = Math.min(...closes), max = Math.max(...closes);
  if (min === max) { min -= 1; max += 1; }
  const n = recs.length;
  const x = i => pad + (n === 1 ? W/2 : i * (W - 2*pad) / (n - 1));
  const y = v => H - pad - (v - min) * (H - 2*pad) / (max - min);
  let d = "", dots = "";
  recs.forEach((r, i) => {
    const xi = x(i);
    if (r.Close === null) { dots += `<circle class="dot" cx="${xi}" cy="${H-pad}" r="3"/>`; return; }
    d += (d ? "L" : "M") + xi.toFixed(1) + "," + y(r.Close).toFixed(1) + " ";
  });
  svg.innerHTML =
    `<path d="${d}" fill="none" stroke="#4c8dff" stroke-width="1.6"/>` + dots;

  // table
  const body = document.querySelector("#table tbody");
  body.innerHTML = recs.map(r =>
    `<tr class="${r.Close === null ? "miss" : ""}">` +
    `<td>${r.Date}</td><td>${fmt(r.Open)}</td><td>${fmt(r.High)}</td>` +
    `<td>${fmt(r.Low)}</td><td>${fmt(r.Close)}</td><td>${fmt(r.Volume)}</td>` +
    `</tr>`).join("");
}
const fmt = v => (v === null || v === undefined) ? "NaN" : Number(v).toLocaleString();
document.getElementById("ranges").addEventListener("click", e => {
  if (e.target.dataset.range) load(e.target.dataset.range);
});
load("30d");
</script>
</body>
</html>
"""


def make_handler(context):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet by default
            pass

        def _send(self, status, body, content_type):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path == "/health":
                self._send(200, "ok", "text/plain; charset=utf-8")
                return
            if parsed.path == "/ohlcv":
                self._handle_ohlcv(params)
                return
            if parsed.path in ("/", "/index.html"):
                self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
                return
            self._send(404, "not found", "text/plain; charset=utf-8")

        def _handle_ohlcv(self, params):
            try:
                scraper = context.build_scraper(params)
                headers, rows = scraper.get_data()
            except Exception as exc:  # surface as 400 with message
                self._send(400, f"error: {exc}", "text/plain; charset=utf-8")
                return

            if params.get("format", ["csv"])[0] == "json":
                body = json.dumps(_rows_to_json(headers, rows), separators=(",", ":"))
                self._send(200, body, "application/json; charset=utf-8")
                return

            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow(headers)
            writer.writerows(rows)
            body = buffer.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "inline")
            self.send_header("X-Missing-Count", str(_missing_count(rows)))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(host, port, context):
    httpd = ThreadingHTTPServer((host, port), make_handler(context))
    print(f"cryptocmd server on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="cryptocmd local OHLCV server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--coin", default="btc")
    parser.add_argument("--fiat", default="usd")
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="serve from offline fixtures (deterministic)",
    )
    parser.add_argument("--fixture-dir", default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--cache-dir", default=None)
    args = parser.parse_args(argv)

    fixture_dir = args.fixture_dir if args.fixtures else None
    if args.exchange == DEFAULT_EXCHANGE and args.fixtures:
        args.exchange = "bitstamp"
    cache_dir = args.cache_dir or tempfile.mkdtemp(prefix="cryptocmd-cache-")
    context = ServiceContext(
        args.exchange,
        args.coin,
        args.fiat,
        fixture_dir=fixture_dir,
        cache_dir=cache_dir,
    )
    serve(args.host, args.port, context)


if __name__ == "__main__":
    main()
