"""
solar_monitor/dashboard.py — HTML dashboard rendering
======================================================
Builds the self-contained HTML dashboard file from DeviceReading data.

The dashboard includes:
  - A totals banner (battery bank V / A / W, PV input, yield today)
  - A battery bank aggregate SoC card (average across all BMS packs)
  - Per-device cards for each BMS and each Victron device
  - Four Chart.js line graphs (battery V, current, PV power, SoC)
  - Three switchable colour themes (dark, light, business) with localStorage persistence

Totals logic:
  Battery V / A / W: BMS readings only.
    Victron devices are excluded to avoid double-counting (the MPPT charger
    and the battery bank sit at the same voltage; mixing them in an average
    is meaningless).
  PV Power In / Yield Today: MPPT solar charger readings only (not inverters).
    Inverter AC output appears on its own card and is not summed here.
"""

import json
import logging
from datetime import datetime
from typing import Optional

from .models import DeviceReading

log = logging.getLogger(__name__)


# ── Utility helpers ───────────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
# HTML dashboard  (unchanged from previous version)
# ═══════════════════════════════════════════════════════════════════════════════

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Solar Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@300;600;800&family=Inter:wght@400;500;600&family=DM+Serif+Display&display=swap');
  :root {
    --bg:#0a0e17;--panel:#111827;--border:#1e2d45;--accent:#00e5ff;--green:#39ff8a;
    --amber:#ffb830;--solar:#ffe066;--red:#ff4060;--violet:#a78bfa;
    --text:#cde4f0;--muted:#4a6375;
    --glow-b:0 0 18px rgba(0,229,255,.25);--glow-g:0 0 18px rgba(57,255,138,.3);
    --glow-a:0 0 18px rgba(255,184,48,.3);--glow-s:0 0 18px rgba(255,224,102,.35);
  }
  body.light {
    --bg:#f0f4f8;--panel:#ffffff;--border:#d1dbe6;--accent:#0077aa;--green:#1a8a4a;
    --amber:#b86800;--solar:#7a6200;--red:#cc2244;--violet:#6644cc;
    --text:#1a2535;--muted:#7a8fa8;
    --glow-b:none;--glow-g:none;--glow-a:none;--glow-s:none;
  }
  body.light header{background:linear-gradient(90deg,#e4edf7,#dce8f4)}
  body.light body::before{display:none}
  body.light .badge.ok{background:rgba(26,138,74,.12);border-color:rgba(26,138,74,.3)}
  body.light .badge.error{background:rgba(204,34,68,.1);border-color:rgba(204,34,68,.3)}
  body.light .badge.warn{background:rgba(184,104,0,.1);border-color:rgba(184,104,0,.3)}
  body.light .type-pill.bms{background:rgba(0,119,170,.08);border-color:rgba(0,119,170,.25)}
  body.light .type-pill.mppt{background:rgba(122,98,0,.08);border-color:rgba(122,98,0,.25)}

  /* ── Business theme ─────────────────────────────────────────────────────── */
  body.business {
    --bg:#f5f4f2;--panel:#ffffff;--border:#e2e0dc;
    --accent:#1d4ed8;--green:#15803d;--amber:#b45309;
    --solar:#92400e;--red:#b91c1c;--violet:#6d28d9;
    --text:#1c1917;--muted:#78716c;
    --glow-b:none;--glow-g:none;--glow-a:none;--glow-s:none;
    font-family:'Inter',sans-serif;
  }
  /* suppress scanline */
  body.business::before { display:none; }

  /* header */
  body.business header {
    background:#ffffff;
    border-bottom:1px solid var(--border);
    box-shadow:0 1px 3px rgba(0,0,0,.06);
    padding:18px 32px;
  }
  body.business .brand {
    font-family:'DM Serif Display',serif;
    font-weight:400; font-size:1.75rem;
    letter-spacing:0; text-shadow:none; text-transform:none;
    color:var(--text);
  }
  body.business .brand span { color:var(--accent); }
  body.business .meta {
    font-family:'Inter',sans-serif; font-size:.75rem;
    color:var(--muted); line-height:1.5;
  }
  body.business .meta strong { color:var(--text); }

  /* totals banner → flex row of stat cards */
  body.business .totals {
    display:flex; flex-wrap:wrap; gap:12px;
    padding:20px 32px; background:none; border:none;
  }
  body.business .total-cell {
    flex:1 1 140px; max-width:220px;
    background:#fff; border:1px solid var(--border);
    border-radius:10px; padding:16px 20px;
    box-shadow:0 1px 3px rgba(0,0,0,.05);
    display:flex; flex-direction:column; gap:4px;
  }
  body.business .total-label {
    font-family:'Inter',sans-serif; font-size:.65rem;
    font-weight:600; letter-spacing:.06em;
    text-transform:uppercase; color:var(--muted);
  }
  body.business .total-value {
    font-family:'Inter',sans-serif; font-size:2rem;
    font-weight:700; line-height:1.1;
    text-shadow:none;
  }
  body.business .total-unit {
    font-family:'Inter',sans-serif; font-size:.65rem; color:var(--muted);
  }

  /* section titles */
  body.business .section-title {
    font-family:'Inter',sans-serif; font-size:.65rem;
    font-weight:700; letter-spacing:.1em; text-transform:uppercase;
    padding:20px 32px 10px; color:var(--muted);
    border-bottom:1px solid var(--border);
  }

  /* cards grid — keep grid so aggregate card can span full width */
  body.business .cards {
    display:grid;
    grid-template-columns:repeat(auto-fill, minmax(280px,1fr));
    gap:12px; padding:16px 32px;
    background:none; border:none;
  }
  body.business .card {
    background:#fff; border:1px solid var(--border);
    border-radius:12px; padding:16px 18px;
    box-shadow:0 1px 4px rgba(0,0,0,.06);
    border-left:none;
    min-width:0;         /* prevent grid blowout */
    overflow:hidden;
  }
  body.business .card.bms       { border-top:3px solid var(--accent); }
  body.business .card.mppt      { border-top:3px solid var(--amber);  }
  body.business .card.aggregate {
    border-top:3px solid var(--violet);
    grid-column:1 / -1;
  }

  /* card header — prevent badge from squeezing name */
  body.business .card-header { gap:8px; align-items:flex-start; }
  body.business .card-name {
    font-family:'Inter',sans-serif; font-weight:600;
    font-size:.85rem; letter-spacing:0; text-transform:none;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }
  body.business .card-addr {
    font-family:'Inter',sans-serif; font-size:.6rem; color:var(--muted);
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }

  /* type pills */
  body.business .type-pill {
    font-family:'Inter',sans-serif; font-size:.58rem;
    font-weight:700; border-radius:4px; letter-spacing:.03em;
    padding:1px 6px;
  }
  body.business .type-pill.bms  { background:rgba(29,78,216,.08); color:var(--accent); border-color:rgba(29,78,216,.25); }
  body.business .type-pill.mppt { background:rgba(180,83,9,.08);  color:var(--amber);  border-color:rgba(180,83,9,.25); }
  body.business .type-pill.inv  { background:rgba(109,40,217,.08);color:var(--violet); border-color:rgba(109,40,217,.25); }
  body.business .type-pill.mon  { background:rgba(21,128,61,.08); color:var(--green);  border-color:rgba(21,128,61,.25); }

  /* status badges */
  body.business .badge {
    font-family:'Inter',sans-serif; font-weight:600;
    font-size:.58rem; border-radius:4px; letter-spacing:.03em;
    padding:2px 7px; white-space:nowrap; flex-shrink:0;
  }
  body.business .badge.ok    { background:rgba(21,128,61,.1);   color:var(--green); border-color:rgba(21,128,61,.3); }
  body.business .badge.error { background:rgba(185,28,28,.08);  color:var(--red);   border-color:rgba(185,28,28,.25); }
  body.business .badge.warn  { background:rgba(180,83,9,.08);   color:var(--amber); border-color:rgba(180,83,9,.25); }

  /* metrics — tighter sizing so 3 values always fit */
  body.business .metrics { gap:6px; margin-bottom:10px; }
  body.business .metric-val {
    font-family:'Inter',sans-serif; font-weight:700;
    font-size:1.2rem; line-height:1;
  }
  body.business .metric-lbl {
    font-family:'Inter',sans-serif; font-weight:500;
    font-size:.58rem; letter-spacing:.04em;
  }

  /* SoC bar */
  body.business .soc-track { background:rgba(0,0,0,.08); }
  body.business .soc-label {
    font-family:'Inter',sans-serif; font-size:.68rem;
    white-space:nowrap;
  }

  /* state row */
  body.business .state-row {
    font-family:'Inter',sans-serif; font-size:.68rem;
    gap:10px;
  }
  body.business .state-k { font-family:'Inter',sans-serif; font-weight:500; }
  body.business .state-v { font-family:'Inter',sans-serif; }

  /* aggregate card */
  body.business .agg-title {
    font-family:'Inter',sans-serif; font-weight:600;
    font-size:.65rem; letter-spacing:.06em; text-transform:uppercase;
  }
  body.business .agg-stat-val {
    font-family:'Inter',sans-serif; font-weight:700; font-size:1.9rem;
  }
  body.business .agg-stat-lbl {
    font-family:'Inter',sans-serif; font-weight:500; font-size:.62rem;
    letter-spacing:.05em; text-transform:uppercase;
  }

  /* error text */
  body.business .error-msg {
    font-family:'Inter',sans-serif; font-size:.72rem;
  }

  /* charts */
  body.business .charts-section { padding:8px 32px 0; }
  body.business .chart-grid { gap:16px; }
  body.business .chart-box {
    border-radius:12px; border:1px solid var(--border);
    box-shadow:0 1px 4px rgba(0,0,0,.05);
    background:#fff;
  }
  body.business .chart-title {
    font-family:'Inter',sans-serif; font-weight:600;
    font-size:.68rem; letter-spacing:.04em; text-transform:uppercase;
    color:var(--muted);
  }

  /* theme button */
  body.business .theme-btn {
    font-family:'Inter',sans-serif; font-weight:500;
    border-radius:6px; font-size:.72rem;
    border-color:var(--border);
  }
  body.business footer { font-family:'Inter',sans-serif; }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Barlow Condensed',sans-serif;
    min-height:100vh;padding-bottom:60px}
  body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:9999;
    background:repeating-linear-gradient(0deg,transparent,transparent 2px,
    rgba(0,0,0,.07) 2px,rgba(0,0,0,.07) 4px)}
  header{display:flex;align-items:center;justify-content:space-between;
    padding:22px 40px 18px;border-bottom:1px solid var(--border);
    background:linear-gradient(90deg,#0a0e17,#0d1829)}
  .brand{font-size:2.1rem;font-weight:800;letter-spacing:.12em;color:var(--accent);
    text-shadow:var(--glow-b);text-transform:uppercase}
  .brand span{color:var(--solar)}
  .meta{font-family:'Share Tech Mono',monospace;font-size:.78rem;color:var(--muted);
    text-align:right;line-height:1.6}
  .meta strong{color:var(--text)}
  .totals{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;
    background:var(--border);border-bottom:1px solid var(--border)}
  .total-cell{background:var(--panel);padding:22px 28px;display:flex;
    flex-direction:column;gap:4px}
  .total-label{font-size:.65rem;letter-spacing:.2em;text-transform:uppercase;color:var(--muted)}
  .total-value{font-family:'Share Tech Mono',monospace;font-size:2.5rem;line-height:1;font-weight:700}
  .total-value.v{color:var(--accent);text-shadow:var(--glow-b)}
  .total-value.a{color:var(--green);text-shadow:var(--glow-g)}
  .total-value.w{color:var(--amber);text-shadow:var(--glow-a)}
  .total-value.pv{color:var(--solar);text-shadow:var(--glow-s)}
  .total-value.yld{color:var(--violet)}
  .total-unit{font-size:.72rem;color:var(--muted);letter-spacing:.06em}
  .section-title{padding:26px 40px 10px;font-size:.68rem;letter-spacing:.25em;
    text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border)}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
    gap:1px;background:var(--border);border-bottom:1px solid var(--border)}
  .card{background:var(--panel);padding:22px 26px}
  .card.mppt{border-left:3px solid var(--solar)}
  .card.bms{border-left:3px solid var(--accent)}
  .card-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px}
  .card-name{font-size:1rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
  .card-addr{font-family:'Share Tech Mono',monospace;font-size:.65rem;color:var(--muted);margin-top:3px}
  .type-pill{font-family:'Share Tech Mono',monospace;font-size:.62rem;padding:2px 8px;
    border-radius:2px;letter-spacing:.1em;margin-bottom:4px;display:inline-block}
  .type-pill.bms{background:rgba(0,229,255,.1);color:var(--accent);border:1px solid rgba(0,229,255,.25)}
  .type-pill.mppt{background:rgba(255,224,102,.1);color:var(--solar);border:1px solid rgba(255,224,102,.25)}
  .type-pill.inv{background:rgba(167,139,250,.1);color:var(--violet);border:1px solid rgba(167,139,250,.25)}
  .type-pill.mon{background:rgba(57,255,138,.1);color:var(--green);border:1px solid rgba(57,255,138,.25)}
  .badge{font-family:'Share Tech Mono',monospace;font-size:.7rem;padding:3px 10px;
    border-radius:2px;letter-spacing:.08em;white-space:nowrap}
  .badge.ok{background:rgba(57,255,138,.12);color:var(--green);border:1px solid rgba(57,255,138,.3)}
  .badge.error{background:rgba(255,64,96,.12);color:var(--red);border:1px solid rgba(255,64,96,.3)}
  .badge.warn{background:rgba(255,184,48,.12);color:var(--amber);border:1px solid rgba(255,184,48,.3)}
  .metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
  .metric{display:flex;flex-direction:column;gap:2px}
  .metric-val{font-family:'Share Tech Mono',monospace;font-size:1.45rem;line-height:1}
  .metric-val.v{color:var(--accent)}.metric-val.a{color:var(--green)}
  .metric-val.w{color:var(--amber)}.metric-val.pv{color:var(--solar)}.metric-val.yld{color:var(--violet)}
  .metric-lbl{font-size:.6rem;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
  .section-lbl{font-size:.58rem;letter-spacing:.22em;text-transform:uppercase;color:var(--muted);
    margin:8px 0 4px;padding-bottom:3px;border-bottom:1px solid rgba(255,255,255,.07);width:100%}
  .soc-row{display:flex;align-items:center;gap:10px}
  .soc-track{flex:1;height:5px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden}
  .soc-fill{height:100%;border-radius:3px;transition:width .5s}
  .soc-label{font-family:'Share Tech Mono',monospace;font-size:.7rem;color:var(--text);white-space:nowrap}
  .state-row{margin-top:10px;font-family:'Share Tech Mono',monospace;font-size:.72rem;
    display:flex;gap:16px;flex-wrap:wrap}
  .state-kv{display:flex;gap:6px}.state-k{color:var(--muted)}.state-v{color:var(--text)}
  .card.aggregate { border-left: 3px solid var(--violet); grid-column: 1 / -1; }
  .agg-title { font-size:.65rem; letter-spacing:.2em; text-transform:uppercase; color:var(--muted); margin-bottom:4px; }
  .agg-summary { display:flex; gap:28px; flex-wrap:wrap; }
  .agg-stat { display:flex; flex-direction:column; gap:2px; }
  .agg-stat-val { font-family:'Share Tech Mono',monospace; font-size:1.6rem; line-height:1; color:var(--violet); }
  .agg-stat-lbl { font-size:.6rem; letter-spacing:.18em; text-transform:uppercase; color:var(--muted); }
  .error-msg{font-family:'Share Tech Mono',monospace;font-size:.72rem;color:var(--red);
    padding:8px 0;word-break:break-all}
  .charts-section{padding:26px 40px 0}
  .chart-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));
    gap:20px;margin-top:16px}
  .chart-box{background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:18px 22px}
  .chart-title{font-size:.65rem;letter-spacing:.2em;text-transform:uppercase;color:var(--muted);margin-bottom:12px}
  canvas{max-height:200px}
  .theme-btn{font-family:'Share Tech Mono',monospace;font-size:.7rem;padding:4px 12px;
    border-radius:2px;border:1px solid var(--border);background:transparent;
    color:var(--muted);cursor:pointer;letter-spacing:.08em;transition:color .2s,border-color .2s}
  .theme-btn:hover{color:var(--text);border-color:var(--text)}
  footer{margin-top:36px;text-align:center;font-family:'Share Tech Mono',monospace;
    font-size:.68rem;color:var(--muted)}
  @media(max-width:700px){header{flex-direction:column;gap:12px;align-items:flex-start}
    .totals{grid-template-columns:repeat(2,1fr)}.chart-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div class="brand">Solar <span>Monitor</span></div>
  <div style="display:flex;align-items:center;gap:16px">
    <button class="theme-btn" onclick="toggleTheme()" id="themeBtn">☀ Light</button>
    <div class="meta"><strong>UPDATED</strong><br>__TIMESTAMP__<br>__BMS_COUNT__ BMS &nbsp;·&nbsp; __MPPT_COUNT__ MPPT</div>
  </div>
</header>
<div class="totals">
  <div class="total-cell"><div class="total-label">Avg Battery V</div><div class="total-value v">__TOTAL_V__</div><div class="total-unit">VOLTS</div></div>
  <div class="total-cell"><div class="total-label">Net Current</div><div class="total-value a">__TOTAL_A__</div><div class="total-unit">AMPS</div></div>
  <div class="total-cell"><div class="total-label">Battery Power</div><div class="total-value w">__TOTAL_W__</div><div class="total-unit">WATTS</div></div>
  <div class="total-cell"><div class="total-label">PV Power In</div><div class="total-value pv">__TOTAL_PV__</div><div class="total-unit">WATTS (all MPPTs)</div></div>
  <div class="total-cell"><div class="total-label">Yield Today</div><div class="total-value yld">__TOTAL_YLD__</div><div class="total-unit">Wh (all MPPTs)</div></div>
</div>
<div class="section-title">Battery Packs — JBD / Vatrer BMS</div>
<div class="cards">__AGG_CARD____BMS_CARDS__</div>
<div class="section-title">Solar Charge Controllers & Inverters — Victron</div>
<div class="cards">__MPPT_CARDS__</div>
<div class="charts-section">
  <div class="section-title" style="padding:0 0 10px;border:none;">Historical Trends</div>
  <div class="chart-grid">
    <div class="chart-box"><div class="chart-title">Battery Voltage (V)</div><canvas id="chartV"></canvas></div>
    <div class="chart-box"><div class="chart-title">Battery Current (A)</div><canvas id="chartA"></canvas></div>
    <div class="chart-box"><div class="chart-title">PV Power — MPPT (W)</div><canvas id="chartPV"></canvas></div>
    <div class="chart-box"><div class="chart-title">State of Charge — BMS (%)</div><canvas id="chartSoC"></canvas></div>
  </div>
</div>
<footer>Refreshes on next poll · Solar Monitor</footer>
<script>
// ── Theme ──────────────────────────────────────────────────────────────────
const SERVER_THEME = '__SERVER_THEME__';
const THEMES = ['dark', 'light', 'business'];
const THEME_LABELS = { dark: '☀ Light', light: '⬡ Business', business: '☽ Dark' };
function applyTheme(t) {
  document.body.classList.remove('light', 'business');
  if (t === 'light')    document.body.classList.add('light');
  if (t === 'business') document.body.classList.add('business');
  document.getElementById('themeBtn').textContent = THEME_LABELS[t] || '☀ Light';
}
function toggleTheme() {
  const cur  = THEMES.find(t => document.body.classList.contains(t)) || 'dark';
  const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
  localStorage.setItem('solarTheme', next);
  applyTheme(next);
}
applyTheme(localStorage.getItem('solarTheme') || SERVER_THEME);
</script>
<script>
const HISTORY=__HISTORY_JSON__;
const PAL=['#00e5ff','#39ff8a','#ffb830','#ff4060','#ffe066','#a78bfa','#fb923c','#34d399'];
function chartOpts() {
  const s = getComputedStyle(document.body);
  const muted  = s.getPropertyValue('--muted').trim()  || '#4a6375';
  const border = s.getPropertyValue('--border').trim() || '#1e2d45';
  const mono   = document.body.classList.contains('business') ? 'Inter' : 'Share Tech Mono';
  return {responsive:true,animation:false,
    plugins:{legend:{labels:{color:muted,font:{family:mono,size:11}}}},
    scales:{x:{ticks:{color:muted,font:{family:mono,size:10}},grid:{color:border}},
            y:{ticks:{color:muted,font:{family:mono,size:10}},grid:{color:border}}}};
}
function chart(id,field,filterFn){
  const keys=Object.keys(HISTORY).filter(filterFn||(()=>true));
  const labels=keys.length?HISTORY[keys[0]].map(r=>r.timestamp.slice(11,19)):[];
  new Chart(document.getElementById(id),{type:'line',data:{labels,
    datasets:keys.map((k,i)=>({label:k,data:HISTORY[k].map(r=>r[field]),
    borderColor:PAL[i%PAL.length],backgroundColor:'transparent',
    borderWidth:1.5,pointRadius:2,tension:0.3}))},options:chartOpts()});
}
chart('chartV','voltage_v');
chart('chartA','current_a');
chart('chartPV','pv_power_w',k=>HISTORY[k].some(r=>r.pv_power_w!=null));
chart('chartSoC','capacity_pct',k=>HISTORY[k].some(r=>r.capacity_pct!=null));
</script>
</body>
</html>
"""


def _soc_color(pct: int) -> str:
    if pct >= 60: return "var(--green)"
    if pct >= 30: return "var(--amber)"
    return "var(--red)"


def _fmt(v, decimals=2, suffix="") -> str:
    return f"{v:.{decimals}f}{suffix}" if v is not None else "—"


def render_bms_card(r: DeviceReading) -> str:
    badge = "error" if r.error else "ok"
    label = "OFFLINE" if r.error else "ONLINE"
    header = (f'<div class="card-header"><div><div class="type-pill bms">BMS</div>'
              f'<div class="card-name">{r.name}</div>'
              f'<div class="card-addr">{r.address}</div></div>'
              f'<div class="badge {badge}">{label}</div></div>')
    if r.error:
        body = f'<div class="error-msg">⚠ {r.error}</div>'
    else:
        soc = r.capacity_pct or 0
        temps = (f'<div class="temps">NTC: {"  ".join(f"{t} °C" for t in r.temp_c)}</div>'
                 if r.temp_c else "")
        body = (f'<div class="metrics">'
                f'<div class="metric"><div class="metric-val v">{_fmt(r.voltage_v,3)}</div><div class="metric-lbl">Volts</div></div>'
                f'<div class="metric"><div class="metric-val a">{_fmt(r.current_a,3)}</div><div class="metric-lbl">Amps</div></div>'
                f'<div class="metric"><div class="metric-val w">{_fmt(r.power_w,2)}</div><div class="metric-lbl">Watts</div></div>'
                f'</div><div class="soc-row">'
                f'<div class="soc-track"><div class="soc-fill" style="width:{soc}%;background:{_soc_color(soc)}"></div></div>'
                f'<div class="soc-label">SoC {soc}% &nbsp;·&nbsp; {r.cell_count or "?"} cells</div>'
                f'</div>{temps}')
    return f'<div class="card bms">{header}{body}</div>'


def render_victron_card(r: DeviceReading) -> str:
    """Render a card for any Victron device (MPPT, Inverter, Battery Monitor, etc.)."""
    partial     = r.error and r.voltage_v is not None
    badge       = "warn" if partial else ("error" if r.error else "ok")
    badge_label = "PARTIAL" if partial else ("OFFLINE" if r.error else "ONLINE")

    # Choose type pill label and colour class
    type_labels = {
        "mppt":     ("MPPT",     "mppt"),
        "inverter": ("INVERTER", "inv"),
        "monitor":  ("MONITOR",  "mon"),
        "meter":    ("METER",    "mon"),
        "dcdc":     ("DC/DC",    "mppt"),
        "lithium":  ("LITHIUM",  "mon"),
    }
    pill_text, pill_cls = type_labels.get(r.device_type, ("VICTRON", "mppt"))

    header = (f'<div class="card-header"><div>'
              f'<div class="type-pill {pill_cls}">{pill_text}</div>'
              f'<div class="card-name">{r.name}</div>'
              f'<div class="card-addr">{r.address}</div></div>'
              f'<div class="badge {badge}">{badge_label}</div></div>')

    if r.error and not partial:
        body = f'<div class="error-msg">⚠ {r.error}</div>'
    else:
        err_note = f'<div class="error-msg" style="margin-top:8px">⚠ {r.error}</div>' if r.error else ""

        if r.device_type == "inverter":
            # Complete alarm bitmask per Victron spec (used for 0x03/0x07 records)
            _ALARM_BITS = {
                0: "Low Batt V", 1: "High Batt V", 2: "Low SOC",
                3: "Low Starter V", 4: "High Starter V", 5: "Low Temp",
                6: "High Temp", 7: "Mid Voltage", 8: "Overload",
                9: "DC Ripple", 10: "Low AC Out V", 11: "High AC Out V",
                12: "Short Circuit", 13: "BMS Lockout",
            }

            state_str = r.inverter_state or "—"

            # ── VE.Bus Smart Dongle — layout matches VictronConnect labels ────
            if r.inverter_state is not None and (
                    r.ac_in_source is not None or r.ac_out_power_va is not None):

                # AC OUTPUT L1 fields
                # Voltage: fixed 120V for this installation (not in payload)
                ac_l1_v   = "120"
                ac_l1_w   = _fmt(r.ac_out_power_va, 0) if r.ac_out_power_va is not None else "—"
                # Current: computed from power ÷ 120V when available
                if r.ac_out_power_va is not None:
                    ac_l1_a = _fmt(r.ac_out_power_va / 120.0, 2)
                else:
                    ac_l1_a = "—"
                # Frequency: not transmitted in this record type
                ac_l1_hz  = "—"

                # Battery fields
                batt_v_str = _fmt(r.voltage_v, 2) if r.voltage_v is not None else "—"
                # Raw signed current: negative = discharging, positive = charging
                batt_a_str = _fmt(r.current_a, 2) if r.current_a is not None else "—"

                # Status row
                temp_str   = f"{r.temperature_c}°C" if r.temperature_c is not None else "—"
                alarm_str  = r.alarm_reason or "None"
                alarm_color = "var(--red)" if r.alarm_reason else "var(--green)"
                ac_in_src  = r.ac_in_source or "—"

                body = (
                    # ── AC OUTPUT L1 ──────────────────────────────────────────
                    f'<div class="section-lbl">AC Output L1</div>'
                    f'<div class="metrics">'
                    f'<div class="metric"><div class="metric-val v">{ac_l1_v}</div>'
                    f'<div class="metric-lbl">Voltage (V)</div></div>'
                    f'<div class="metric"><div class="metric-val w">{ac_l1_w}</div>'
                    f'<div class="metric-lbl">Power (W)</div></div>'
                    f'<div class="metric"><div class="metric-val pv">{ac_l1_a}</div>'
                    f'<div class="metric-lbl">Current (A)</div></div>'
                    f'</div>'
                    # ── Battery ───────────────────────────────────────────────
                    f'<div class="section-lbl">Battery</div>'
                    f'<div class="metrics">'
                    f'<div class="metric"><div class="metric-val v">{batt_v_str}</div>'
                    f'<div class="metric-lbl">Voltage (V)</div></div>'
                    f'<div class="metric"><div class="metric-val w">{batt_a_str}</div>'
                    f'<div class="metric-lbl">Current (A)</div></div>'
                    f'<div class="metric"><div class="metric-val pv">{temp_str}</div>'
                    f'<div class="metric-lbl">Temperature</div></div>'
                    f'</div>'
                    # ── Status row ────────────────────────────────────────────
                    f'<div class="state-row">'
                    f'<div class="state-kv"><span class="state-k">STATE</span>'
                    f'<span class="state-v">{state_str}</span></div>'
                    f'<div class="state-kv"><span class="state-k">AC In</span>'
                    f'<span class="state-v">{ac_in_src}</span></div>'
                    f'<div class="state-kv">'
                    f'<span class="state-k" style="color:{alarm_color}">ALARM</span>'
                    f'<span class="state-v" style="color:{alarm_color}">{alarm_str}</span>'
                    f'</div></div>'
                    f'{err_note}'
                )

            # ── Standard inverter (record 0x03/0x07) ──────────────────────────
            else:
                alarm_val   = r.alarm_reason if isinstance(r.alarm_reason, int) else 0
                alarms      = [lbl for bit, lbl in sorted(_ALARM_BITS.items())
                               if alarm_val & (1 << bit)]
                alarm_str   = ", ".join(alarms) if alarms else "None"
                alarm_color = "var(--red)" if alarms else "var(--green)"

                if r.ac_out_power_va is not None:
                    power_display = _fmt(r.ac_out_power_va, 0)
                    power_label   = "AC Out W"
                elif r.raw_load_indicator is not None:
                    power_display = f"~{r.raw_load_indicator}"
                    power_label   = "Load (raw)"
                else:
                    power_display = "—"
                    power_label   = "AC Out W"

                ac_a_display = (
                    _fmt(r.ac_out_current_a, 2) if r.ac_out_current_a is not None
                    else f"raw={r.raw_load_indicator}" if r.raw_load_indicator is not None
                    else "—"
                )

                body = (
                    f'<div class="metrics">'
                    f'<div class="metric"><div class="metric-val v">{_fmt(r.voltage_v, 2)}</div>'
                    f'<div class="metric-lbl">DC Batt V</div></div>'
                    f'<div class="metric"><div class="metric-val w">{power_display}</div>'
                    f'<div class="metric-lbl">{power_label}</div></div>'
                    f'<div class="metric"><div class="metric-val pv">{_fmt(r.ac_out_voltage_v, 1)}</div>'
                    f'<div class="metric-lbl">AC Out V</div></div>'
                    f'</div>'
                    f'<div class="state-row">'
                    f'<div class="state-kv"><span class="state-k">STATE</span>'
                    f'<span class="state-v">{state_str}</span></div>'
                    f'<div class="state-kv"><span class="state-k">AC Out A</span>'
                    f'<span class="state-v">{ac_a_display}</span></div>'
                    f'<div class="state-kv">'
                    f'<span class="state-k" style="color:{alarm_color}">ALARM</span>'
                    f'<span class="state-v" style="color:{alarm_color}">{alarm_str}</span>'
                    f'</div></div>'
                    f'{err_note}'
                )
        elif r.device_type == "monitor":
            # Battery Monitor (SmartShunt / BMV): V, A, SoC, TTG
            soc = r.capacity_pct or 0
            ttg = f"{r.ttg_minutes // 60}h {r.ttg_minutes % 60}m" if r.ttg_minutes is not None else "—"
            body = (f'<div class="metrics">'
                    f'<div class="metric"><div class="metric-val v">{_fmt(r.voltage_v,3)}</div><div class="metric-lbl">Batt V</div></div>'
                    f'<div class="metric"><div class="metric-val a">{_fmt(r.current_a,3)}</div><div class="metric-lbl">Amps</div></div>'
                    f'<div class="metric"><div class="metric-val w">{_fmt(r.power_w,2)}</div><div class="metric-lbl">Watts</div></div>'
                    f'</div><div class="soc-row">'
                    f'<div class="soc-track"><div class="soc-fill" style="width:{soc}%;background:{_soc_color(soc)}"></div></div>'
                    f'<div class="soc-label">SoC {soc}% &nbsp;·&nbsp; TTG {ttg}</div>'
                    f'</div>{err_note}')
        else:
            # Solar Charger (MPPT) card
            load_str = (f"{_fmt(r.load_current_a, 1)}A"
                        if getattr(r, "load_current_a", None) is not None else None)
            body = (f'<div class="metrics">'
                    f'<div class="metric"><div class="metric-val v">{_fmt(r.voltage_v,3)}</div><div class="metric-lbl">Batt V</div></div>'
                    f'<div class="metric"><div class="metric-val a">{_fmt(r.current_a,3)}</div><div class="metric-lbl">Batt A</div></div>'
                    f'<div class="metric"><div class="metric-val pv">{_fmt(r.pv_power_w,1)}</div><div class="metric-lbl">PV W</div></div>'
                    f'</div><div class="state-row">'
                    f'<div class="state-kv"><span class="state-k">STATE</span><span class="state-v">{r.charger_state or "—"}</span></div>'
                    f'<div class="state-kv"><span class="state-k">YIELD</span><span class="state-v">{_fmt(r.yield_today_wh,0)} Wh</span></div>'
                    + (f'<div class="state-kv"><span class="state-k">LOAD</span><span class="state-v">{load_str}</span></div>' if load_str else '')
                    + (f'<div class="state-kv"><span class="state-k">ERR</span><span class="state-v">{r.error_code}</span></div>' if r.error_code else '')
                    + f'</div>{err_note}')

    return f'<div class="card mppt">{header}{body}</div>'


def _no_card(msg: str) -> str:
    return (f'<div class="card" style="color:var(--muted);font-family:\'Share Tech Mono\','
            f'monospace;font-size:.8rem;padding:30px">{msg}</div>')


def render_bms_aggregate_card(bms_readings: list) -> str:
    """Render a summary card showing the average SoC across all BMS packs."""
    ok     = [r for r in bms_readings if not r.error and r.capacity_pct is not None]
    total  = len(bms_readings)
    online = len(ok)

    avg_soc = round(sum(r.capacity_pct for r in ok) / len(ok)) if ok else None
    color   = _soc_color(avg_soc) if avg_soc is not None else "var(--muted)"
    pct_str = f"{avg_soc}%" if avg_soc is not None else "—"

    bar = (
        f'<div style="margin:18px 0 6px">'
        f'<div class="soc-track" style="height:12px;border-radius:6px">'
        f'<div class="soc-fill" style="width:{avg_soc or 0}%;background:{color};'
        f'border-radius:6px;transition:width 1s ease"></div>'
        f'</div></div>'
    )

    stats = (
        f'<div class="agg-summary">'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val" style="font-size:3rem;color:{color}">{pct_str}</div>'
        f'<div class="agg-stat-lbl">Average SoC</div>'
        f'</div>'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val">{online}/{total}</div>'
        f'<div class="agg-stat-lbl">Packs online</div>'
        f'</div>'
        f'</div>'
    )

    body = f'<div class="agg-title">Battery Bank — State of Charge</div>{bar}{stats}'
    return f'<div class="card aggregate">{body}</div>'


def build_html(bms_readings: list, mppt_readings: list, history: dict, theme: str = "dark") -> str:
    """
    Render the complete HTML dashboard as a string.

    Totals banner logic:
    - Avg Battery V / Net Current / Battery Power: BMS readings only.
      Victron devices are excluded to avoid double-counting (the MPPT charger
      and the battery sit at the same voltage; averaging them is meaningless).
    - PV Power In / Yield Today: MPPT solar charger readings only (not inverters).
      Inverter AC output is shown on its own card, not summed here.

    Args:
        bms_readings:  DeviceReading list from JBD/Vatrer BMS devices.
        mppt_readings: DeviceReading list from Victron devices (all types).
        history:       Rolling dict of {device_name: [reading_dict, …]} for charts.
        theme:         CSS theme class applied to <body>: "dark", "light", or "business".

    Returns:
        Complete HTML document as a string, ready to write to disk.
    """
    ok_bms  = [r for r in bms_readings  if not r.error and r.voltage_v is not None]
    ok_mppt = [r for r in mppt_readings if not r.error]

    # Battery voltage and current totals are BMS-only.
    # Including Victron devices would double-count: the MPPT charger and the battery
    # both sit at ~53V — averaging them inflates the count and muddles the meaning.
    # Battery Power = sum of BMS pack watts (V × I per pack).
    total_v   = _fmt(sum(r.voltage_v for r in ok_bms) / len(ok_bms), 3) if ok_bms else "—"
    total_a   = _fmt(sum(r.current_a for r in ok_bms if r.current_a is not None), 3)
    total_w   = _fmt(sum(r.power_w   for r in ok_bms if r.power_w   is not None), 2)

    # PV power = sum of MPPT solar charger output watts only (not inverter AC output).
    ok_mppt_solar = [r for r in ok_mppt if r.device_type == "mppt"]
    ok_mppt_inv   = [r for r in ok_mppt if r.device_type == "inverter"]
    total_pv  = _fmt(sum(r.pv_power_w    for r in ok_mppt_solar if r.pv_power_w    is not None), 1)
    total_yld = _fmt(sum(r.yield_today_wh for r in ok_mppt_solar if r.yield_today_wh is not None), 0)
    # Inverter AC output power shown separately in its card; not summed into battery totals

    agg_card  = render_bms_aggregate_card(bms_readings) if bms_readings else ""
    bms_html  = "".join(render_bms_card(r)      for r in bms_readings)  or _no_card("No JBD / Vatrer BMS devices found")
    mppt_html = "".join(render_victron_card(r)   for r in mppt_readings) or _no_card("No Victron devices found")

    return (HTML_TEMPLATE
            .replace("__TIMESTAMP__",    datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
            .replace("__BMS_COUNT__",    str(len(bms_readings)))
            .replace("__MPPT_COUNT__",   str(len(mppt_readings)))
            .replace("__TOTAL_V__",      total_v)
            .replace("__TOTAL_A__",      total_a)
            .replace("__TOTAL_W__",      total_w)
            .replace("__TOTAL_PV__",     total_pv)
            .replace("__TOTAL_YLD__",    total_yld)
            .replace("__AGG_CARD__",     agg_card)
            .replace("__BMS_CARDS__",    bms_html)
            .replace("__MPPT_CARDS__",   mppt_html)
            .replace("__SERVER_THEME__", theme)
            .replace("__HISTORY_JSON__", json.dumps(history, indent=2)))


