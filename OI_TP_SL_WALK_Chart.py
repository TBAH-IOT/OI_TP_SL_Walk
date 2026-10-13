"""Local Hyperliquid-style full 5m candle chart for OI trades.

Run:
    python OI_TP_SL_FULL_CHART.py

The script starts a local viewer at http://127.0.0.1:8765 and opens it.
"""

from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parent
CACHE_ROOT = PACKAGE_ROOT / "data"
TRADES_DEFAULT = (
    CACHE_ROOT
    / "oi_tp_sl_walk_hyperliquid_close_entry_open_t1_top20_train_may_test_june"
    / "trades.csv"
)
OI_DEFAULT = CACHE_ROOT / "gfi_oi_5m_30coins_cache" / "oi_5m_30coins_combined.csv"
OHLC_DEFAULT = CACHE_ROOT / "hl_ohlc_5m"
DEFAULT_WALLET_BALANCE = 1000.0


PAGE = r'''<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OI q01 Full 5m Chart</title>
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{color-scheme:dark;--bg:#0d1117;--panel:#151b23;--line:#27313d;--fg:#e6edf3;--muted:#8b98a8;--green:#26c281;--red:#ef5350;--orange:#f0a34b;--blue:#5b9cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px Segoe UI,Arial,sans-serif}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:9px;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--panel)}
.brand{font-weight:600;margin-right:8px;white-space:nowrap}
.field{display:flex;align-items:center;gap:6px}.field label{color:var(--muted);font-size:12px}
select,input,button{height:30px;border:1px solid var(--line);border-radius:4px;background:#10161e;color:var(--fg);padding:0 9px;font:inherit}
input[type=datetime-local]{width:168px;color-scheme:dark}input[type=number]{width:105px;text-align:right}
button{cursor:pointer}button:hover{border-color:var(--blue)}
.nav{display:flex;gap:5px;align-items:center}.nav button{min-width:30px;padding:0}
.trade-count{min-width:76px;text-align:center;color:var(--muted);font-variant-numeric:tabular-nums}
.status{margin-left:auto;color:var(--muted);font-size:12px}
.wallet-bar{display:flex;flex-wrap:wrap;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);background:#111820}
.wallet-card{min-width:150px;padding:6px 10px;border:1px solid var(--line);border-radius:5px;background:#151b23}
.wallet-card span{display:block;color:var(--muted);font-size:11px}.wallet-card strong{display:block;margin-top:2px;font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.chart-shell{position:relative;width:100%;height:calc(100vh - 292px);min-height:420px}
.chart{position:absolute;inset:0}
.crosshair{position:absolute;right:12px;top:8px;padding:5px 8px;color:var(--muted);background:rgba(21,27,35,.9);border:1px solid var(--line);border-radius:4px;pointer-events:none;font-variant-numeric:tabular-nums}
.bottom{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:10px;padding:10px 12px;border-top:1px solid var(--line);background:var(--panel)}
.legend{color:var(--muted);font-size:12px;line-height:1.65}.legend b{color:var(--fg);font-weight:500}
.detail{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px}.detail div{min-width:0}
.detail span{display:block;color:var(--muted);font-size:11px}.detail strong{display:block;margin-top:2px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.positive{color:var(--green)!important}.negative{color:var(--red)!important}.hint{margin-top:7px;color:var(--muted);font-size:11px}
.table-wrap{margin-top:7px;max-height:142px;overflow:auto;border:1px solid var(--line);border-radius:4px}
.trade-table{width:100%;border-collapse:collapse;font-size:11px;font-variant-numeric:tabular-nums}.trade-table th,.trade-table td{padding:4px 6px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:right}.trade-table th:first-child,.trade-table td:first-child,.trade-table th:nth-child(2),.trade-table td:nth-child(2){text-align:left}.trade-table th{position:sticky;top:0;background:#1a222c;color:var(--muted);font-weight:500}.trade-table tr{cursor:pointer}.trade-table tr:hover,.trade-table tr.active{background:#1b2a35}
@media(max-width:980px){.status{width:100%;margin-left:0}.chart-shell{height:calc(100vh - 330px);min-height:400px}.bottom{grid-template-columns:1fr}.detail{grid-template-columns:repeat(3,1fr)}}
@media(max-width:620px){input[type=datetime-local]{width:155px}.wallet-card{min-width:135px}.detail{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="top">
  <div class="brand">OI q01 · TP/SL · Hyperliquid 5m</div>
  <div class="field"><label for="symbol">Coin</label><select id="symbol"></select></div>
  <div class="field"><label for="startAt">Từ</label><input id="startAt" type="datetime-local"></div>
  <div class="field"><label for="endAt">Đến</label><input id="endAt" type="datetime-local"></div>
  <div class="field"><label for="initialBalance">Ví đầu</label><input id="initialBalance" type="number" min="0.01" step="0.01" value="1000"></div>
  <button id="apply" type="button">Áp dụng</button>
  <div class="nav"><button id="previous" aria-label="Lệnh trước">↑</button><span id="tradeCount" class="trade-count">0 / 0</span><button id="next" aria-label="Lệnh sau">↓</button></div>
  <button id="fit" type="button">Toàn bộ</button>
  <button id="focus" type="button">Quanh lệnh</button>
  <button id="recent" type="button">30 ngày</button>
  <span id="status" class="status">Đang tải…</span>
</div>
<div class="wallet-bar">
  <div class="wallet-card"><span>Số dư ví cuối kỳ</span><strong id="walletBalance">—</strong></div>
  <div class="wallet-card"><span>Biến động ví</span><strong id="walletPnl">—</strong></div>
  <div class="wallet-card"><span>Số lệnh toàn bộ top coin</span><strong id="walletTrades">—</strong></div>
  <div class="wallet-card"><span>Số dư sau lệnh đang xem</span><strong id="selectedBalance">—</strong></div>
</div>
<div class="chart-shell">
  <div id="chart" class="chart"></div>
  <div id="crosshair" class="crosshair">Di chuột lên chart để xem giá</div>
</div>
<div class="bottom">
  <div>
    <div class="legend"><b>🟠 T</b> trigger OI tại nến t · <b>🟢 E</b> vào tại open nến t+1 · <b>🔻 X</b> thoát lệnh · Chọn một dòng để xem rõ 3 điểm và thông số lệnh.</div>
    <div class="hint">Giá chart, trigger và entry dùng OHLC Hyperliquid. Khoảng thời gian lọc theo thời điểm vào lệnh; phí đã nằm trong PnL.</div>
    <div class="table-wrap"><table class="trade-table"><thead><tr><th>#</th><th>Trigger</th><th>Entry</th><th>Exit</th><th>Loại</th><th>PnL</th><th>Ví sau lệnh</th></tr></thead><tbody id="tradeTable"></tbody></table></div>
  </div>
  <div id="detail" class="detail"></div>
</div>
<script>
const symbolSelect = document.getElementById('symbol');
const startAt = document.getElementById('startAt');
const endAt = document.getElementById('endAt');
const initialBalance = document.getElementById('initialBalance');
const statusEl = document.getElementById('status');
const detail = document.getElementById('detail');
const tradeTable = document.getElementById('tradeTable');
const tradeCount = document.getElementById('tradeCount');
const crosshair = document.getElementById('crosshair');
const walletBalance = document.getElementById('walletBalance');
const walletPnl = document.getElementById('walletPnl');
const walletTrades = document.getElementById('walletTrades');
const selectedBalance = document.getElementById('selectedBalance');

const chart = LightweightCharts.createChart(document.getElementById('chart'), {
  layout: { background: { type: 'solid', color: '#0d1117' }, textColor: '#8b98a8' },
  grid: { vertLines: { color: '#1c2631' }, horzLines: { color: '#1c2631' } },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: '#27313d' },
  timeScale: { borderColor: '#27313d', timeVisible: true, secondsVisible: false },
  handleScroll: { mouseWheel: true, pressedMouseMove: true },
  handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true }
});
const candleSeries = chart.addCandlestickSeries({
  upColor: '#26c281', downColor: '#ef5350', borderUpColor: '#26c281',
  borderDownColor: '#ef5350', wickUpColor: '#26c281', wickDownColor: '#ef5350',
  priceLineVisible: false
});

let current = { symbol: '', strategy: 'TP_SL', trades: [], candles: [], wallet: {} };
let tradePos = 0;
let tradePriceLines = [];

function fmtTime(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  return new Intl.DateTimeFormat('vi-VN', {
    timeZone: 'Asia/Bangkok', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit'
  }).format(new Date(seconds * 1000));
}
function fmtDateInput(seconds) {
  const d = new Date(seconds * 1000);
  const pad = (x) => String(x).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
    'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}
function parseDateInput(value) {
  if (!value) return null;
  const valueMs = new Date(value).getTime();
  return Number.isFinite(valueMs) ? Math.floor(valueMs / 1000) : null;
}
function fmtPrice(x) {
  if (x === null || x === undefined || !Number.isFinite(Number(x))) return '—';
  return Number(x) >= 100 ? Number(x).toFixed(2) : Number(x).toFixed(5);
}
function fmtPct(x) { return x === null || x === undefined ? '—' : (Number(x) * 100).toFixed(3) + '%'; }
function fmtUsd(x) {
  if (x === null || x === undefined || !Number.isFinite(Number(x))) return '—';
  return Number(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' USD';
}
function signedUsd(x) { return (Number(x) >= 0 ? '+' : '') + fmtUsd(x); }
function setClass(element, value) {
  element.classList.remove('positive', 'negative');
  element.classList.add(Number(value) >= 0 ? 'positive' : 'negative');
}

function showWallet() {
  const wallet = current.wallet || {};
  walletBalance.textContent = fmtUsd(wallet.final);
  walletPnl.textContent = signedUsd(wallet.pnl || 0);
  setClass(walletPnl, wallet.pnl || 0);
  walletTrades.textContent = Number(wallet.trades || 0).toLocaleString('en-US');
  const selected = current.trades[tradePos];
  selectedBalance.textContent = selected ? fmtUsd(selected.wallet_after) : '—';
  if (selected) setClass(selectedBalance, selected.wallet_change);
  else selectedBalance.classList.remove('positive', 'negative');
}

function showDetail() {
  const t = current.trades[tradePos];
  if (!t) {
    detail.innerHTML = '<div><span>Chi tiết lệnh</span><strong>Không có lệnh trong khoảng chọn</strong></div>';
    tradeCount.textContent = '0 / 0';
    showWallet();
    return;
  }
  tradeCount.textContent = (tradePos + 1) + ' / ' + current.trades.length;
  const cls = t.pnl >= 0 ? 'positive' : 'negative';
  detail.innerHTML =
    '<div><span>Coin / loại thoát</span><strong>' + t.symbol + ' · ' + t.exit_type + '</strong></div>' +
    '<div><span>Lệnh toàn ví</span><strong>#' + t.wallet_number + ' · sau lệnh ' + fmtUsd(t.wallet_after) + '</strong></div>' +
    '<div><span>Trigger OI · giá close nến t</span><strong>' + fmtTime(t.trigger_time) + ' · ' + fmtPrice(t.trigger_price) + '</strong></div>' +
    '<div><span>Entry · open nến t+1</span><strong>' + fmtTime(t.entry_time) + ' · ' + fmtPrice(t.entry_price) + '</strong></div>' +
    '<div><span>Exit</span><strong>' + fmtTime(t.exit_time) + ' · ' + fmtPrice(t.exit_price) + '</strong></div>' +
    '<div><span>Gross → Net</span><strong>' + fmtPct(t.gross) + ' → ' + fmtPct(t.net) + '</strong></div>' +
    '<div><span>PnL ròng</span><strong class="' + cls + '">' + signedUsd(t.pnl) + '</strong></div>' +
    '<div><span>TP / SL · OI trigger</span><strong>' + fmtPct(t.tp) + ' / ' + fmtPct(t.sl) + ' · ' + (t.oi_usd ? fmtUsd(t.oi_usd) : '—') + '</strong></div>' +
    '<div><span>Ví trước → sau</span><strong>' + fmtUsd(t.wallet_before) + ' → ' + fmtUsd(t.wallet_after) + '</strong></div>';
  showWallet();
}

function renderTradeTable() {
  tradeTable.innerHTML = current.trades.map((t, index) =>
    '<tr data-index="' + index + '" class="' + (index === tradePos ? 'active' : '') + '">' +
    '<td>' + (index + 1) + '</td>' +
    '<td>' + fmtTime(t.trigger_time) + '</td>' +
    '<td>' + fmtPrice(t.entry_price) + '</td>' +
    '<td>' + fmtTime(t.exit_time) + '</td>' +
    '<td>' + t.exit_type + '</td>' +
    '<td class="' + (t.pnl >= 0 ? 'positive' : 'negative') + '">' + signedUsd(t.pnl) + '</td>' +
    '<td>' + fmtUsd(t.wallet_after) + '</td></tr>'
  ).join('');
  tradeTable.querySelectorAll('tr').forEach((row) => {
    row.onclick = () => {
      tradePos = Number(row.dataset.index);
      renderTradeTable();
      showDetail();
      showTradeLines();
      focusTrade();
    };
  });
}

function clearTradeLines() {
  tradePriceLines.forEach((line) => candleSeries.removePriceLine(line));
  tradePriceLines = [];
}
function showTradeLines() {
  clearTradeLines();
  const t = current.trades[tradePos];
  if (!t) return;
  const lines = [
    ['trigger_price', '#f0a34b', 'TRIGGER', LightweightCharts.LineStyle.Dotted],
    ['entry_price', '#26c281', 'ENTRY', LightweightCharts.LineStyle.Dashed],
    ['exit_price', '#ef5350', 'EXIT ' + t.exit_type, LightweightCharts.LineStyle.Solid]
  ];
  lines.forEach(([key, color, title, lineStyle]) => {
    if (t[key] === null || t[key] === undefined || !Number.isFinite(Number(t[key]))) return;
    tradePriceLines.push(candleSeries.createPriceLine({
      price: Number(t[key]), color, lineWidth: 1, lineStyle,
      axisLabelVisible: true, title
    }));
  });
}

function makeMarkers(trades) {
  const markers = [];
  trades.forEach((t) => {
    markers.push({ time: t.trigger_time, position: 'aboveBar', color: '#f0a34b', shape: 'circle', text: 'T' });
    markers.push({ time: t.entry_time, position: 'belowBar', color: '#26c281', shape: 'arrowUp', text: 'E' });
    markers.push({ time: t.exit_time, position: 'aboveBar', color: '#ef5350', shape: 'arrowDown', text: 'X ' + t.exit_type });
  });
  return markers.sort((a, b) => a.time - b.time);
}

function focusTrade() {
  const t = current.trades[tradePos];
  if (!t) return;
  chart.timeScale().setVisibleRange({
    from: Math.max(0, t.trigger_time - 21600),
    to: t.exit_time + 21600
  });
}

async function loadData() {
  const symbol = symbolSelect.value;
  const start = parseDateInput(startAt.value);
  const end = parseDateInput(endAt.value);
  const balance = Number(initialBalance.value);
  statusEl.textContent = 'Đang tải ' + symbol + '…';
  const query = '/api/data?symbol=' + encodeURIComponent(symbol) +
    '&start=' + encodeURIComponent(start === null ? '' : start) +
    '&end=' + encodeURIComponent(end === null ? '' : end) +
    '&balance=' + encodeURIComponent(balance);
  const response = await fetch(query);
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  current = {
    symbol: symbol, strategy: 'TP_SL', trades: data.trades, candles: data.candles,
    wallet: data.wallet, range_start: data.range_start, range_end: data.range_end
  };
  tradePos = 0;
  candleSeries.setData(data.candles);
  candleSeries.setMarkers(makeMarkers(data.trades));
  chart.timeScale().fitContent();
  renderTradeTable();
  showDetail();
  showTradeLines();
  statusEl.textContent = data.candles.length.toLocaleString('en-US') + ' nến · ' + data.trades.length + ' lệnh trên ' + symbol + ' · ví tính trên toàn bộ coin';
}

function reload() { loadData().catch((e) => { statusEl.textContent = e.message; }); }
symbolSelect.onchange = reload;
document.getElementById('apply').onclick = reload;
document.getElementById('fit').onclick = () => chart.timeScale().fitContent();
document.getElementById('focus').onclick = focusTrade;
document.getElementById('recent').onclick = () => {
  const end = parseDateInput(endAt.value) || current.range_end || Math.floor(Date.now() / 1000);
  startAt.value = fmtDateInput(end - 30 * 86400);
  endAt.value = fmtDateInput(end);
  reload();
};
document.getElementById('previous').onclick = () => {
  tradePos = Math.max(0, tradePos - 1);
  renderTradeTable(); showDetail(); showTradeLines(); focusTrade();
};
document.getElementById('next').onclick = () => {
  tradePos = Math.min(Math.max(0, current.trades.length - 1), tradePos + 1);
  renderTradeTable(); showDetail(); showTradeLines(); focusTrade();
};
document.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') document.getElementById('previous').click();
  if (event.key === 'ArrowRight' || event.key === 'ArrowDown') document.getElementById('next').click();
});
chart.subscribeCrosshairMove((param) => {
  if (!param || !param.time || !param.seriesData.has(candleSeries)) {
    crosshair.textContent = 'Di chuột lên chart để xem giá';
    return;
  }
  const c = param.seriesData.get(candleSeries);
  crosshair.textContent = fmtTime(Number(param.time)) + ' · O ' + fmtPrice(c.open) +
    ' H ' + fmtPrice(c.high) + ' L ' + fmtPrice(c.low) + ' C ' + fmtPrice(c.close);
});

async function init() {
  const meta = await (await fetch('/api/meta')).json();
  meta.coins.forEach((s) => {
    const option = document.createElement('option');
    option.value = s; option.textContent = s; symbolSelect.appendChild(option);
  });
  symbolSelect.value = meta.coins[0];
  initialBalance.value = meta.initial_balance;
  startAt.value = fmtDateInput(meta.range_start);
  endAt.value = fmtDateInput(meta.range_end);
  await loadData();
}
init().catch((e) => { statusEl.textContent = e.message; });
</script>
</body>
</html>'''


def load_candles(ohlc_dir: Path, symbol: str) -> list[dict]:
    path = ohlc_dir / f"{symbol}_ohlc_5m.csv"
    if not path.exists():
        raise FileNotFoundError(f"Thiếu OHLC cache: {path}")
    frame = pd.read_csv(path)
    frame["bucket_5m"] = pd.to_datetime(frame["bucket_5m"], utc=True)
    frame = frame.sort_values("bucket_5m").drop_duplicates("bucket_5m")
    return [
        {
            "time": int(row.bucket_5m.timestamp()),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
        }
        for row in frame.itertuples(index=False)
    ]


def load_trades(trades_path: Path, oi_path: Path) -> pd.DataFrame:
    trades = pd.read_csv(trades_path)
    if "strategy" in trades.columns:
        trades = trades[trades["strategy"].eq("TP_SL")].copy()
    if trades.empty:
        raise RuntimeError(f"Không có giao dịch TP_SL trong {trades_path}")
    trades["entry_time"] = pd.to_datetime(trades["entry_time"], utc=True)
    trades["exit_time"] = pd.to_datetime(trades["exit_time"], utc=True)

    numeric_columns = [
        "gross_return",
        "net_return",
        "pnl_usd",
        "selected_tp",
        "selected_sl",
    ]
    for column in numeric_columns:
        if column in trades.columns:
            trades[column] = pd.to_numeric(trades[column], errors="coerce")
    trades = trades.sort_values(["entry_time", "exit_time", "symbol"]).reset_index(drop=True)
    trades["trade_id"] = range(len(trades))
    return trades


def load_oi_values(oi_path: Path) -> dict[tuple[str, int], float]:
    if not oi_path.exists():
        return {}
    oi = pd.read_csv(oi_path)
    required = {"symbol", "bucket_5m", "oi_usd"}
    if not required.issubset(oi.columns):
        return {}
    oi["bucket_5m"] = pd.to_datetime(oi["bucket_5m"], utc=True)
    oi["oi_usd"] = pd.to_numeric(oi["oi_usd"], errors="coerce")
    return {
        (str(row.symbol), int(row.bucket_5m.timestamp())): float(row.oi_usd)
        for row in oi.dropna(subset=["oi_usd"]).itertuples(index=False)
    }


class App:
    def __init__(
        self,
        trades_path: Path,
        oi_path: Path,
        ohlc_dir: Path,
        default_balance: float,
    ):
        self.trades = load_trades(trades_path, oi_path)
        self.oi_values = load_oi_values(oi_path)
        self.ohlc_dir = ohlc_dir
        self.default_balance = float(default_balance)
        self.candle_cache: dict[str, list[dict]] = {}
        self.candle_lookup: dict[str, dict[int, dict]] = {}
        self.coins = sorted(
            symbol
            for symbol in self.trades["symbol"].dropna().astype(str).unique().tolist()
            if (self.ohlc_dir / f"{symbol}_ohlc_5m.csv").exists()
        )
        if not self.coins:
            raise RuntimeError(f"Không tìm thấy OHLC Hyperliquid cho các coin trong {trades_path}")
        self.min_entry_s = int(self.trades["entry_time"].min().timestamp())
        self.max_entry_s = int(self.trades["entry_time"].max().timestamp())

    def candles_for(self, symbol: str) -> list[dict]:
        if symbol not in self.candle_cache:
            candles = load_candles(self.ohlc_dir, symbol)
            self.candle_cache[symbol] = candles
            self.candle_lookup[symbol] = {int(item["time"]): item for item in candles}
        return self.candle_cache[symbol]

    @staticmethod
    def _range_timestamp(value: float | None, fallback: int) -> pd.Timestamp:
        return pd.to_datetime(fallback if value is None else value, unit="s", utc=True)

    def data(
        self,
        symbol: str,
        start_s: float | None = None,
        end_s: float | None = None,
        initial_balance: float | None = None,
    ) -> dict:
        if symbol not in self.coins:
            raise ValueError(f"Coin không hợp lệ: {symbol}")
        all_candles = self.candles_for(symbol)
        lookup = self.candle_lookup[symbol]
        start = self._range_timestamp(start_s, self.min_entry_s)
        end = self._range_timestamp(end_s, self.max_entry_s)
        if start > end:
            raise ValueError("Thời điểm bắt đầu phải trước thời điểm kết thúc")
        balance_start = self.default_balance if initial_balance is None else float(initial_balance)
        if not pd.api.types.is_number(balance_start) or balance_start <= 0:
            raise ValueError("Số dư ban đầu phải lớn hơn 0")

        in_range = self.trades[
            (self.trades["entry_time"] >= start)
            & (self.trades["entry_time"] <= end)
        ].copy()
        wallet_rows = in_range.sort_values(["exit_time", "entry_time", "symbol"])
        wallet_state: dict[int, tuple[float, float, int]] = {}
        balance = float(balance_start)
        for number, row in enumerate(wallet_rows.itertuples(index=False), start=1):
            before = balance
            change = float(row.pnl_usd)
            balance += change
            wallet_state[int(row.trade_id)] = (before, balance, number)

        selected = in_range[in_range["symbol"].astype(str).eq(symbol)].sort_values(
            ["entry_time", "exit_time"]
        )

        output_trades = []
        for row in selected.itertuples(index=False):
            entry_time_s = int(row.entry_time.timestamp())
            exit_time_s = int(row.exit_time.timestamp())
            trigger_time_s = entry_time_s - 5 * 60
            trigger_candle = lookup.get(trigger_time_s)
            entry_candle = lookup.get(entry_time_s)
            exit_candle = lookup.get(exit_time_s)
            entry_price = float(entry_candle["open"]) if entry_candle else None
            trigger_price = float(trigger_candle["close"]) if trigger_candle else None
            if row.exit_type == "TIME_60M" and exit_candle:
                exit_price = float(exit_candle["close"])
            elif entry_price is not None:
                exit_price = entry_price * (1.0 + float(row.gross_return))
            else:
                exit_price = None
            wallet_before, wallet_after, wallet_number = wallet_state[int(row.trade_id)]
            output_trades.append(
                {
                    "symbol": symbol,
                    "trigger_time": trigger_time_s,
                    "trigger_price": trigger_price,
                    "entry_time": entry_time_s,
                    "entry_price": entry_price,
                    "exit_time": exit_time_s,
                    "exit_price": exit_price,
                    "exit_type": str(row.exit_type),
                    "gross": float(row.gross_return),
                    "net": float(row.net_return),
                    "pnl": float(row.pnl_usd),
                    "tp": float(row.selected_tp) if pd.notna(row.selected_tp) else None,
                    "sl": float(row.selected_sl) if pd.notna(row.selected_sl) else None,
                    "oi_usd": self.oi_values.get((symbol, trigger_time_s)),
                    "wallet_before": wallet_before,
                    "wallet_after": wallet_after,
                    "wallet_change": float(row.pnl_usd),
                    "wallet_number": wallet_number,
                }
            )
        candle_start_s = int(start.timestamp()) - 6 * 3600
        candle_end_s = int(end.timestamp()) + 6 * 3600
        if not in_range.empty:
            candle_end_s = max(candle_end_s, int(in_range["exit_time"].max().timestamp()) + 6 * 3600)
        candles = [
            candle
            for candle in all_candles
            if candle_start_s <= int(candle["time"]) <= candle_end_s
        ]
        return {
            "symbol": symbol,
            "strategy": "TP_SL",
            "candles": candles,
            "trades": output_trades,
            "range_start": int(start.timestamp()),
            "range_end": int(end.timestamp()),
            "wallet": {
                "initial": float(balance_start),
                "final": float(balance),
                "pnl": float(balance - balance_start),
                "trades": int(len(wallet_rows)),
            },
        }


class Handler(BaseHTTPRequestHandler):
    app: App
    page: bytes

    def send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(self.page, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/meta":
            payload = json.dumps(
                {
                    "coins": self.app.coins,
                    "strategy": "TP_SL",
                    "range_start": self.app.min_entry_s,
                    "range_end": self.app.max_entry_s,
                    "initial_balance": self.app.default_balance,
                }
            ).encode("utf-8")
            self.send_bytes(payload, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/data":
            query = parse_qs(parsed.query)
            symbol = query.get("symbol", [self.app.coins[0]])[0]
            def optional_float(name: str) -> float | None:
                value = query.get(name, [""])[0].strip()
                return float(value) if value else None

            try:
                payload = json.dumps(
                    self.app.data(
                        symbol,
                        start_s=optional_float("start"),
                        end_s=optional_float("end"),
                        initial_balance=optional_float("balance"),
                    ),
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_bytes(payload, "application/json; charset=utf-8")
            except Exception as exc:
                self.send_bytes(
                    str(exc).encode("utf-8"),
                    "text/plain; charset=utf-8",
                    400,
                )
            return
        self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="OI full chart viewer")
    parser.add_argument("--trades", type=Path, default=TRADES_DEFAULT)
    parser.add_argument("--oi", type=Path, default=OI_DEFAULT)
    parser.add_argument("--ohlc-dir", type=Path, default=OHLC_DEFAULT)
    parser.add_argument(
        "--initial-balance",
        type=float,
        default=DEFAULT_WALLET_BALANCE,
        help="Số dư ví ban đầu hiển thị trên chart",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    app = App(args.trades, args.oi, args.ohlc_dir, args.initial_balance)
    Handler.app = app
    Handler.page = PAGE.encode("utf-8")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Coins: {', '.join(app.coins)}")
    print(f"Viewer: {url}")
    print("Nhan Ctrl+C de dung viewer.")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nDa dung viewer.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

