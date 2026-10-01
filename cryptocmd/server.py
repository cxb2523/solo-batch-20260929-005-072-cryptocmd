"""Local OHLCV server for cryptocmd.

    python -m cryptocmd.server [--host 127.0.0.1] [--port 8000]

Endpoints:
    GET /health  -> plain text "ok"
    GET /ohlcv   -> CSV in the CMC schema. Query params:
                    exchange (default "bitstamp"), symbol (default "btcusd"),
                    start / end (dd-mm-yyyy or yyyy-mm-dd), fiat.
    GET /        -> market page with range switching and a missing-point
                    counter (handy for screen recordings).

Stdlib-only: no extra dependencies.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import parse_qs, urlparse

from .exchanges import available_exchanges, get_exchange

DEFAULT_PORT = 8000

_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>cryptocmd 行情</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; background: #0f1420; color: #e6e9f0; }
  h1 { font-size: 1.3rem; }
  .bar { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; margin-bottom: 1rem; }
  button, select, input { background: #1c2436; color: #e6e9f0; border: 1px solid #33405c;
    border-radius: 6px; padding: .4rem .8rem; cursor: pointer; }
  button.active { background: #3b82f6; border-color: #3b82f6; }
  #stats { margin: .6rem 0; font-variant-numeric: tabular-nums; }
  #stats b { color: #fbbf24; }
  canvas { width: 100%; height: 420px; background: #131a2b; border: 1px solid #33405c; border-radius: 8px; }
  #err { color: #f87171; white-space: pre-wrap; }
</style>
</head>
<body>
<h1>cryptocmd 本地行情</h1>
<div class="bar">
  <label>交易所 <select id="exchange"></select></label>
  <label>交易对 <input id="symbol" value="btcusd" size="8"></label>
  <span id="ranges"></span>
  <span id="stats"></span>
</div>
<canvas id="chart" width="1200" height="420"></canvas>
<div id="err"></div>
<script>
const RANGES = [["7D", 7], ["30D", 30], ["90D", 90], ["1Y", 365]];
let currentDays = 30;

const fmt = d => String(d.getUTCDate()).padStart(2, "0") + "-" +
  String(d.getUTCMonth() + 1).padStart(2, "0") + "-" + d.getUTCFullYear();

function parseCsv(text) {
  const rows = [];
  let field = "", row = [], inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQ) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; } else inQ = false;
      } else field += c;
    } else if (c === '"') inQ = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\\n" || c === "\\r") {
      if (c === "\\r" && text[i + 1] === "\\n") i++;
      row.push(field); field = "";
      if (row.length > 1 || row[0] !== "") rows.push(row);
      row = [];
    } else field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows;
}

async function load() {
  const err = document.getElementById("err");
  err.textContent = "";
  const end = new Date();
  const start = new Date(end.getTime() - (currentDays - 1) * 86400000);
  const q = new URLSearchParams({
    exchange: document.getElementById("exchange").value,
    symbol: document.getElementById("symbol").value,
    start: fmt(start), end: fmt(end),
  });
  let text;
  try {
    const resp = await fetch("/ohlcv?" + q);
    text = await resp.text();
    if (!resp.ok) throw new Error(resp.status + " " + text);
  } catch (e) { err.textContent = "加载失败: " + e.message; return; }

  const rows = parseCsv(text);
  const data = rows.slice(1).map(r => ({
    date: r[0], close: r[4] === "nan" || r[4] === "" ? NaN : parseFloat(r[4]),
  })).reverse();  // CSV 是最新在前，翻转为时间正序
  const missing = data.filter(d => Number.isNaN(d.close)).length;
  document.getElementById("stats").innerHTML =
    "数据点: <b>" + data.length + "</b> 缺失点: <b>" + missing + "</b>";
  draw(data);
}

function draw(data) {
  const cv = document.getElementById("chart"), ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, pad = 48;
  ctx.clearRect(0, 0, W, H);
  const valid = data.filter(d => !Number.isNaN(d.close));
  if (!valid.length) return;
  const lo = Math.min(...valid.map(d => d.close)), hi = Math.max(...valid.map(d => d.close));
  const x = i => pad + (W - 2 * pad) * i / Math.max(data.length - 1, 1);
  const y = v => H - pad - (H - 2 * pad) * (v - lo) / Math.max(hi - lo, 1e-9);
  ctx.strokeStyle = "#33405c"; ctx.fillStyle = "#8b94ab"; ctx.font = "12px sans-serif";
  for (let g = 0; g <= 4; g++) {
    const v = lo + (hi - lo) * g / 4, yy = y(v);
    ctx.beginPath(); ctx.moveTo(pad, yy); ctx.lineTo(W - pad, yy); ctx.stroke();
    ctx.fillText(v.toFixed(2), 4, yy + 4);
  }
  // 缺失点用红点标在底部
  ctx.fillStyle = "#f87171";
  data.forEach((d, i) => { if (Number.isNaN(d.close)) {
    ctx.beginPath(); ctx.arc(x(i), H - pad + 12, 3, 0, 7); ctx.fill();
  }});
  ctx.strokeStyle = "#3b82f6"; ctx.lineWidth = 2; ctx.beginPath();
  let started = false;
  data.forEach((d, i) => {
    if (Number.isNaN(d.close)) { started = false; return; }
    if (started) ctx.lineTo(x(i), y(d.close)); else ctx.moveTo(x(i), y(d.close));
    started = true;
  });
  ctx.stroke();
  ctx.fillStyle = "#8b94ab";
  ctx.fillText(data[0].date, pad, H - pad + 24);
  ctx.fillText(data[data.length - 1].date, W - pad - 70, H - pad + 24);
}

const rangesEl = document.getElementById("ranges");
RANGES.forEach(([label, days]) => {
  const b = document.createElement("button");
  b.textContent = label;
  if (days === currentDays) b.classList.add("active");
  b.onclick = () => {
    currentDays = days;
    document.querySelectorAll("#ranges button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    load();
  };
  rangesEl.appendChild(b);
});

fetch("/exchanges").then(r => r.json()).then(list => {
  const sel = document.getElementById("exchange");
  list.forEach(name => {
    const o = document.createElement("option");
    o.value = o.textContent = name;
    sel.appendChild(o);
  });
  sel.value = list.includes("bitstamp") ? "bitstamp" : list[0];
  load();
});
document.getElementById("symbol").onchange = load;
document.getElementById("exchange").onchange = load;
</script>
</body>
</html>
"""


class OhlcvHandler(BaseHTTPRequestHandler):
    server_version = "cryptocmd/1.0"

    def _send(self, status, body, content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._send(status, message)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if path == "/health":
            self._send(200, "ok")
            return

        if path == "/exchanges":
            self._send(
                200, json.dumps(available_exchanges()), "application/json"
            )
            return

        if path == "/ohlcv":
            exchange = params.get("exchange", ["bitstamp"])[0]
            symbol = params.get("symbol", [None])[0]
            start = params.get("start", [None])[0]
            end = params.get("end", [None])[0]
            fiat = params.get("fiat", ["USD"])[0]
            try:
                backend = get_exchange(exchange)
            except KeyError as exc:
                self._error(404, str(exc))
                return
            try:
                headers, rows = backend.fetch_ohlcv(
                    symbol, start_date=start, end_date=end, fiat=fiat
                )
            except Exception as exc:  # noqa: BLE001 - surface backend errors as 502
                self._error(502, f"upstream error: {exc}")
                return
            from .exchanges.bitstamp import render_csv

            self._send(200, render_csv(headers, rows), "text/csv; charset=utf-8")
            return

        if path == "/":
            self._send(200, _PAGE, "text/html; charset=utf-8")
            return

        self._error(404, "not found")

    def log_message(self, fmt, *args):  # quieter logs
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cryptocmd.server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), OhlcvHandler)
    print(f"cryptocmd server on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
