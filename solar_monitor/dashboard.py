"""
solar_monitor/dashboard.py - HTML dashboard rendering
======================================================
Builds the self-contained HTML dashboard file from DeviceReading data.

Layout (top to bottom):
  1. Header bar (brand, timestamp, theme toggle)
  2. Aggregate cards row - MPPT totals, Inverter totals, Battery totals
  3. Individual MPPT solar charger cards
  4. Individual Inverter cards
  5. Individual BMS pack cards
  6. Historical chart section (battery V, current, PV power, SoC)

Three switchable colour themes: dark, light, business (localStorage persistence).
"""

import html
import json
import logging
from datetime import datetime
from pathlib import Path
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
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0a0e17">
<title>Solar Monitor</title>
<script>__CHARTJS__</script>
<style>

/* ── Design tokens ── */
:root {
  --bg:#0a0e17; --panel:#111827; --border:#1e2d45;
  --accent:#00e5ff; --green:#39ff8a; --amber:#ffb830;
  --solar:#ffe066; --red:#ff4060; --violet:#a78bfa;
  --text:#cde4f0; --muted:#4a6375;
  --glow-b:0 0 18px rgba(0,229,255,.22);
  --radius:12px; --radius-sm:6px;
  --gap:12px; --page-pad:16px;
}
@media(min-width:768px){ :root{ --page-pad:28px; --gap:16px; } }
@media(min-width:1024px){ :root{ --page-pad:40px; } }

/* ── Light theme ── */
body.light {
  --bg:#f0f4f8; --panel:#fff; --border:#d1dbe6;
  --accent:#0077aa; --green:#1a8a4a; --amber:#b86800;
  --solar:#7a6200; --red:#cc2244; --violet:#6644cc;
  --text:#1a2535; --muted:#7a8fa8; --glow-b:none;
}
body.light header { background:linear-gradient(135deg,#e4edf7,#dce8f4); }
body.light::before { display:none; }
body.light .badge.ok    { background:rgba(26,138,74,.12);  border-color:rgba(26,138,74,.3); }
body.light .badge.error { background:rgba(204,34,68,.1);   border-color:rgba(204,34,68,.3); }
body.light .badge.warn  { background:rgba(184,104,0,.1);   border-color:rgba(184,104,0,.3); }
body.light .soc-track   { background:rgba(0,0,0,.1); }

/* ── Business theme ── */
body.business {
  --bg:#f5f4f2; --panel:#fff; --border:#e2e0dc;
  --accent:#1d4ed8; --green:#15803d; --amber:#b45309;
  --solar:#92400e; --red:#b91c1c; --violet:#6d28d9;
  --text:#1c1917; --muted:#78716c; --glow-b:none;
  font-family:'Inter',sans-serif;
}
body.business::before { display:none; }
body.business header {
  background:#fff; border-bottom:1px solid var(--border);
  box-shadow:0 1px 4px rgba(0,0,0,.06);
}
body.business .brand { font-family:'DM Serif Display',serif; font-weight:400; letter-spacing:0; text-transform:none; color:var(--text); }
body.business .brand span { color:var(--accent); }
body.business .meta { font-family:'Inter',sans-serif; }
body.business .section-title { font-family:'Inter',sans-serif; font-weight:700; letter-spacing:.08em; }
body.business .card { border-radius:var(--radius); box-shadow:0 1px 4px rgba(0,0,0,.06); border-left:none; }
body.business .card.bms      { border-top:3px solid var(--accent); }
body.business .card.mppt     { border-top:3px solid var(--amber); }
body.business .card.agg-mppt { border-top:3px solid var(--solar); }
body.business .card.agg-inv  { border-top:3px solid var(--violet); }
body.business .card.agg-bat  { border-top:3px solid var(--accent); }
body.business .agg-cards { background:none; border:none; gap:var(--gap); padding:var(--gap) var(--page-pad); }
body.business .cards      { background:none; border:none; gap:var(--gap); padding:var(--gap) var(--page-pad); }
body.business .card-name  { font-family:'Inter',sans-serif; font-weight:600; letter-spacing:0; text-transform:none; }
body.business .card-addr  { font-family:'Inter',sans-serif; }
body.business .type-pill  { font-family:'Inter',sans-serif; font-weight:700; border-radius:4px; letter-spacing:.03em; }
body.business .type-pill.bms  { background:rgba(29,78,216,.08);  color:var(--accent); border-color:rgba(29,78,216,.25); }
body.business .type-pill.mppt { background:rgba(180,83,9,.08);   color:var(--amber);  border-color:rgba(180,83,9,.25); }
body.business .type-pill.inv  { background:rgba(109,40,217,.08); color:var(--violet); border-color:rgba(109,40,217,.25); }
body.business .type-pill.mon  { background:rgba(21,128,61,.08);  color:var(--green);  border-color:rgba(21,128,61,.25); }
body.business .badge      { font-family:'Inter',sans-serif; font-weight:600; border-radius:4px; }
body.business .badge.ok   { background:rgba(21,128,61,.1);  color:var(--green); border-color:rgba(21,128,61,.3); }
body.business .badge.error{ background:rgba(185,28,28,.08); color:var(--red);   border-color:rgba(185,28,28,.25); }
body.business .badge.warn { background:rgba(180,83,9,.08);  color:var(--amber); border-color:rgba(180,83,9,.25); }
body.business .metric-val { font-family:'Inter',sans-serif; font-weight:700; }
body.business .metric-lbl { font-family:'Inter',sans-serif; font-weight:500; }
body.business .agg-title  { font-family:'Inter',sans-serif; font-weight:600; letter-spacing:.06em; }
body.business .agg-big    { font-family:'Inter',sans-serif; font-weight:700; letter-spacing:-.02em; }
body.business .agg-big-unit { font-family:'Inter',sans-serif; }
body.business .agg-stat-val { font-family:'Inter',sans-serif; font-weight:700; }
body.business .agg-stat-lbl { font-family:'Inter',sans-serif; font-weight:500; }
body.business .state-row  { font-family:'Inter',sans-serif; }
body.business .temps      { font-family:'Inter',sans-serif; }
body.business .soc-label  { font-family:'Inter',sans-serif; }
body.business .soc-track  { background:rgba(0,0,0,.08); }
body.business .error-msg  { font-family:'Inter',sans-serif; }
body.business .theme-btn  { font-family:'Inter',sans-serif; font-weight:600; border-radius:8px; }
body.business .chart-box  { background:#fff; border-color:var(--border); border-radius:var(--radius); }
body.business .chart-title{ font-family:'Inter',sans-serif; font-weight:600; letter-spacing:.04em; }
body.business footer { font-family:'Inter',sans-serif; }

/* ── Reset ── */
*,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
html { -webkit-text-size-adjust:100%; }

/* ── Body & scanlines ── */
body {
  background:var(--bg); color:var(--text);
  font-family:'Barlow Condensed',sans-serif;
  min-height:100vh; padding-bottom:env(safe-area-inset-bottom,24px);
  overscroll-behavior-y:contain;
}
body::before {
  content:''; position:fixed; inset:0; pointer-events:none; z-index:9999;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.06) 2px,rgba(0,0,0,.06) 4px);
}

/* ── Sticky header ── */
header {
  position:sticky; top:0; z-index:100;
  display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;
  padding:12px var(--page-pad);
  background:linear-gradient(135deg,#0a0e17,#0d1829);
  border-bottom:1px solid var(--border);
  backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
}
@media(min-width:768px){ header{ padding:18px var(--page-pad); } }

.brand {
  font-size:1.6rem; font-weight:800; letter-spacing:.1em;
  color:var(--accent); text-shadow:var(--glow-b); text-transform:uppercase;
  line-height:1;
}
@media(min-width:768px){ .brand{ font-size:2rem; } }
.brand span { color:var(--solar); }

.header-right { display:flex; align-items:center; gap:10px; flex-shrink:0; }

.meta {
  font-family:'Share Tech Mono',monospace; font-size:.7rem; color:var(--muted);
  text-align:right; line-height:1.5;
  display:none;
}
@media(min-width:480px){ .meta{ display:block; } }
.meta strong { color:var(--text); }

/* ── Theme button ── */
.theme-btn {
  font-family:'Share Tech Mono',monospace; font-size:.68rem;
  padding:8px 14px; min-height:44px;
  border-radius:var(--radius-sm); border:1px solid var(--border);
  background:rgba(255,255,255,.05); color:var(--muted); cursor:pointer;
  letter-spacing:.06em; transition:color .2s, border-color .2s, background .2s;
  white-space:nowrap;
}
.theme-btn:hover { color:var(--text); border-color:var(--text); background:rgba(255,255,255,.08); }

/* ── Section titles ── */
.section-title {
  padding:18px var(--page-pad) 8px;
  font-size:.62rem; letter-spacing:.2em; text-transform:uppercase;
  color:var(--muted); border-bottom:1px solid var(--border);
}
@media(min-width:768px){ .section-title{ padding:22px var(--page-pad) 10px; } }

/* ── Aggregate cards — stacked mobile, side-by-side ≥ 600px ── */
.agg-cards {
  display:flex; flex-direction:column; gap:1px;
  background:var(--border); border-bottom:1px solid var(--border);
}
@media(min-width:600px){
  .agg-cards { flex-direction:row; flex-wrap:wrap; }
  .agg-cards>.card { flex:1 1 220px; }
}
.card.agg-mppt { background:var(--panel); padding:18px var(--page-pad); border-left:3px solid var(--solar); }
.card.agg-inv  { background:var(--panel); padding:18px var(--page-pad); border-left:3px solid var(--violet); }
.card.agg-bat  { background:var(--panel); padding:18px var(--page-pad); border-left:3px solid var(--accent); }
@media(min-width:768px){
  .card.agg-mppt,.card.agg-inv,.card.agg-bat { padding:22px 28px; }
}

/* ── Aggregate card internals ── */
.agg-title {
  font-size:.62rem; letter-spacing:.18em; text-transform:uppercase;
  color:var(--muted); margin-bottom:10px;
}
@media(min-width:768px){ .agg-title{ margin-bottom:14px; } }

.agg-big {
  font-family:'Share Tech Mono',monospace;
  font-size:clamp(2rem, 6vw, 3.2rem);
  line-height:1; font-weight:700; letter-spacing:-.01em;
}
.agg-big-unit { font-family:'Share Tech Mono',monospace; font-size:.78rem; color:var(--muted); }
.agg-summary  { display:flex; gap:18px; flex-wrap:wrap; margin-top:12px; }
@media(min-width:768px){ .agg-summary{ gap:28px; } }
.agg-stat { display:flex; flex-direction:column; gap:2px; }
.agg-stat-val { font-family:'Share Tech Mono',monospace; font-size:clamp(1.1rem,4vw,1.6rem); line-height:1; }
.agg-stat-lbl { font-size:.58rem; letter-spacing:.15em; text-transform:uppercase; color:var(--muted); }

/* ── Individual device card grid ── */
.cards {
  display:grid;
  grid-template-columns:1fr;
  gap:1px; background:var(--border); border-bottom:1px solid var(--border);
}
@media(min-width:480px){ .cards{ grid-template-columns:repeat(2,1fr); } }
@media(min-width:900px){ .cards{ grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); } }

/* ── Individual card ── */
.card { background:var(--panel); padding:16px var(--page-pad); overflow:hidden; }
@media(min-width:768px){ .card{ padding:20px 24px; } }
.card.mppt { border-left:3px solid var(--solar); }
.card.bms  { border-left:3px solid var(--accent); }

/* ── Card header ── */
.card-header {
  display:flex; justify-content:space-between; align-items:flex-start;
  margin-bottom:14px; gap:8px;
}
.card-name {
  font-size:.95rem; font-weight:600; letter-spacing:.05em; text-transform:uppercase;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.card-addr {
  font-family:'Share Tech Mono',monospace; font-size:.6rem;
  color:var(--muted); margin-top:2px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.type-pill {
  font-family:'Share Tech Mono',monospace; font-size:.6rem; padding:2px 7px;
  border-radius:2px; letter-spacing:.1em; margin-bottom:3px; display:inline-block;
}
.type-pill.bms  { background:rgba(0,229,255,.1);   color:var(--accent); border:1px solid rgba(0,229,255,.25); }
.type-pill.mppt { background:rgba(255,224,102,.1); color:var(--solar);  border:1px solid rgba(255,224,102,.25); }
.type-pill.inv  { background:rgba(167,139,250,.1); color:var(--violet); border:1px solid rgba(167,139,250,.25); }
.type-pill.mon  { background:rgba(57,255,138,.1);  color:var(--green);  border:1px solid rgba(57,255,138,.25); }

/* ── Status badge ── */
.badge {
  font-family:'Share Tech Mono',monospace; font-size:.65rem; padding:3px 9px;
  border-radius:2px; letter-spacing:.07em; white-space:nowrap; flex-shrink:0;
}
.badge.ok    { background:rgba(57,255,138,.12); color:var(--green); border:1px solid rgba(57,255,138,.3); }
.badge.error { background:rgba(255,64,96,.12);  color:var(--red);   border:1px solid rgba(255,64,96,.3); }
.badge.warn  { background:rgba(255,184,48,.12); color:var(--amber); border:1px solid rgba(255,184,48,.3); }

/* ── Metrics row ── */
.metrics {
  display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:12px;
}
.metric { display:flex; flex-direction:column; gap:1px; }
.metric-val {
  font-family:'Share Tech Mono',monospace;
  font-size:clamp(1rem,4vw,1.45rem); line-height:1;
}
.metric-val.v   { color:var(--accent); }
.metric-val.a   { color:var(--green); }
.metric-val.w   { color:var(--amber); }
.metric-val.pv  { color:var(--solar); }
.metric-val.yld { color:var(--violet); }
.metric-lbl {
  font-size:.56rem; letter-spacing:.14em; text-transform:uppercase; color:var(--muted);
}
.section-lbl {
  font-size:.56rem; letter-spacing:.18em; text-transform:uppercase; color:var(--muted);
  margin:8px 0 4px; padding-bottom:3px;
  border-bottom:1px solid rgba(255,255,255,.06); width:100%;
}

/* ── SoC bar ── */
.soc-row    { display:flex; align-items:center; gap:8px; margin:10px 0 6px; }
.soc-track  { flex:1; height:6px; background:rgba(255,255,255,.07); border-radius:4px; overflow:hidden; min-width:0; }
.soc-fill   { height:100%; border-radius:4px; transition:width .6s ease; }
.soc-label  {
  font-family:'Share Tech Mono',monospace; font-size:.68rem;
  color:var(--text); white-space:nowrap; flex-shrink:0;
}

/* ── State / info rows ── */
.state-row {
  margin-top:8px; font-family:'Share Tech Mono',monospace; font-size:.68rem;
  display:flex; gap:12px; flex-wrap:wrap; row-gap:4px;
}
.state-kv { display:flex; gap:5px; }
.state-k  { color:var(--muted); }
.state-v  { color:var(--text); }

/* ── Text detail rows (temps, faults, balance) ── */
.temps {
  font-family:'Share Tech Mono',monospace; font-size:.68rem;
  color:var(--muted); margin-top:5px; line-height:1.5;
}

/* ── Error message ── */
.error-msg {
  font-family:'Share Tech Mono',monospace; font-size:.7rem; color:var(--red);
  padding:8px 0; word-break:break-word;
}

/* ── Charts ── */
.charts-section { padding:18px var(--page-pad) 0; }
@media(min-width:768px){ .charts-section{ padding:24px var(--page-pad) 0; } }
.chart-grid {
  display:grid; grid-template-columns:1fr;
  gap:12px; margin-top:12px;
}
@media(min-width:600px){  .chart-grid{ grid-template-columns:repeat(2,1fr); } }
@media(min-width:1200px){ .chart-grid{ grid-template-columns:repeat(4,1fr); } }
.chart-box {
  background:var(--panel); border:1px solid var(--border);
  border-radius:var(--radius); padding:14px 16px;
}
.chart-title {
  font-size:.6rem; letter-spacing:.18em; text-transform:uppercase;
  color:var(--muted); margin-bottom:10px;
}
canvas { max-height:180px; width:100% !important; }

/* ── Footer ── */
footer {
  margin-top:32px; margin-bottom:8px;
  text-align:center; font-family:'Share Tech Mono',monospace;
  font-size:.64rem; color:var(--muted);
  padding:0 var(--page-pad);
}

/* ── No-card placeholder ── */
.no-card {
  background:var(--panel); color:var(--muted);
  font-family:'Share Tech Mono',monospace; font-size:.75rem;
  padding:24px var(--page-pad); border-bottom:1px solid var(--border);
}
</style>
</head>
<body>
<header>
  <div class="brand">Solar <span>Monitor</span></div>
  <div class="header-right">
    <button class="theme-btn" onclick="toggleTheme()" id="themeBtn">☀ Light</button>
    <div class="meta"><strong>UPDATED</strong><br>__TIMESTAMP__<br>__BMS_COUNT__ BMS &nbsp;·&nbsp; __MPPT_COUNT__ Victron</div>
  </div>
</header>

<div class="section-title">System Overview</div>
<div class="agg-cards">__MPPT_AGG____INV_AGG____BAT_AGG__</div>

<div class="section-title">MPPT Chargers — Individual</div>
<div class="cards">__MPPT_CARDS__</div>
<div class="section-title">Inverters — Individual</div>
<div class="cards">__INV_CARDS__</div>
<div class="section-title">Battery Packs — Individual</div>
<div class="cards">__BMS_CARDS__</div>

<div class="charts-section">
  <div class="section-title" style="padding:0 0 8px;border:none">Historical Trends</div>
  <div class="chart-grid">
    <div class="chart-box"><div class="chart-title">Battery Voltage (V)</div><canvas id="chartV"></canvas></div>
    <div class="chart-box"><div class="chart-title">Battery Current (A)</div><canvas id="chartA"></canvas></div>
    <div class="chart-box"><div class="chart-title">PV Power — MPPT (W)</div><canvas id="chartPV"></canvas></div>
    <div class="chart-box"><div class="chart-title">State of Charge — BMS (%)</div><canvas id="chartSoC"></canvas></div>
  </div>
</div>
<footer>Solar Monitor · refreshes on next poll</footer>

<script>
// ── Theme ──
const SERVER_THEME='__SERVER_THEME__';
const THEMES=['dark','light','business'];
const LABELS={dark:'☀ Light',light:'⬡ Business',business:'☽ Dark'};
function applyTheme(t){
  document.body.classList.remove('light','business');
  if(t==='light')    document.body.classList.add('light');
  if(t==='business') document.body.classList.add('business');
  document.getElementById('themeBtn').textContent=LABELS[t]||'☀ Light';
  document.querySelector('meta[name="theme-color"]').content=
    t==='dark'?'#0a0e17':t==='light'?'#dce8f4':'#f5f4f2';
}
function toggleTheme(){
  const cur=THEMES.find(t=>document.body.classList.contains(t))||'dark';
  const next=THEMES[(THEMES.indexOf(cur)+1)%THEMES.length];
  localStorage.setItem('solarTheme',next);
  applyTheme(next);
}
applyTheme(localStorage.getItem('solarTheme')||SERVER_THEME);
</script>
<script>
const HISTORY=__HISTORY_JSON__;
const PAL=['#00e5ff','#39ff8a','#ffb830','#ff4060','#ffe066','#a78bfa','#fb923c','#34d399'];
function chartOpts(){
  const s=getComputedStyle(document.body);
  const muted =s.getPropertyValue('--muted').trim()||'#4a6375';
  const border=s.getPropertyValue('--border').trim()||'#1e2d45';
  const isBiz =document.body.classList.contains('business');
  const font  =isBiz?'Inter':'Share Tech Mono';
  return{responsive:true,animation:false,maintainAspectRatio:true,
    plugins:{legend:{labels:{color:muted,font:{family:font,size:10},
      boxWidth:10,padding:8}}},
    scales:{
      x:{ticks:{color:muted,font:{family:font,size:9},maxTicksLimit:6,maxRotation:0},
         grid:{color:border}},
      y:{ticks:{color:muted,font:{family:font,size:9}},
         grid:{color:border}}}};
}
function chart(id,field,filterFn){
  const keys=Object.keys(HISTORY).filter(filterFn||(()=>true));
  const labels=keys.length?HISTORY[keys[0]].map(r=>r.timestamp.slice(11,16)):[];
  new Chart(document.getElementById(id),{type:'line',data:{labels,
    datasets:keys.map((k,i)=>({label:k,data:HISTORY[k].map(r=>r[field]),
      borderColor:PAL[i%PAL.length],backgroundColor:'transparent',
      borderWidth:1.5,pointRadius:1,tension:.3}))},options:chartOpts()});
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


_CHARTJS_PATH = Path(__file__).parent / "vendor" / "chart.umd.min.js"
_chartjs_cache: Optional[str] = None


def _chartjs() -> str:
    """Vendored Chart.js source, loaded once. The dashboard must render
    without internet access, so no CDN references are allowed."""
    global _chartjs_cache
    if _chartjs_cache is None:
        try:
            _chartjs_cache = _CHARTJS_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            log.error(f"Vendored Chart.js missing ({exc}); charts disabled")
            _chartjs_cache = ""
    return _chartjs_cache


def _esc(v) -> str:
    """HTML-escape any value interpolated into markup.

    Device names, error strings, and state labels originate outside this
    module (config, BLE advertisements, exception text) and must never be
    trusted as HTML.
    """
    return html.escape(str(v), quote=True)


def render_bms_card(r: DeviceReading) -> str:
    badge = "error" if r.error else "ok"
    label = "OFFLINE" if r.error else "ONLINE"
    header = (f'<div class="card-header"><div><div class="type-pill bms">BMS</div>'
              f'<div class="card-name">{_esc(r.name)}</div>'
              f'<div class="card-addr">{_esc(r.address)}</div></div>'
              f'<div class="badge {badge}">{label}</div></div>')
    if r.error:
        body = f'<div class="error-msg">⚠ {_esc(r.error)}</div>'
    else:
        soc = r.capacity_pct or 0

        # ── Main metrics row ──────────────────────────────────────────────────
        body = (
            f'<div class="metrics">'
            f'<div class="metric"><div class="metric-val v">{_fmt(r.voltage_v, 3)}</div>'
            f'<div class="metric-lbl">Volts</div></div>'
            f'<div class="metric"><div class="metric-val a">{_fmt(r.current_a, 3)}</div>'
            f'<div class="metric-lbl">Amps</div></div>'
            f'<div class="metric"><div class="metric-val w">{_fmt(r.power_w, 2)}</div>'
            f'<div class="metric-lbl">Watts</div></div>'
            f'</div>'
        )

        # ── SoC bar ───────────────────────────────────────────────────────────
        body += (
            f'<div class="soc-row">'
            f'<div class="soc-track">'
            f'<div class="soc-fill" style="width:{soc}%;background:{_soc_color(soc)}"></div>'
            f'</div>'
            f'<div class="soc-label">SoC {soc}%'
            f'{f" &nbsp;·&nbsp; {r.remain_wh:.0f} Wh" if r.remain_wh else ""}'
            f'</div></div>'
        )

        # ── Capacity / runtime row ────────────────────────────────────────────
        cap_parts = []
        if r.remain_ah is not None and r.nominal_ah is not None:
            cap_parts.append(f'{r.remain_ah:.1f} / {r.nominal_ah:.1f} Ah')
        if r.time_to_empty_h is not None:
            h, m = divmod(int(r.time_to_empty_h * 60), 60)
            cap_parts.append(f'TTE {h}h{m:02d}m')
        if r.time_to_full_h is not None:
            h, m = divmod(int(r.time_to_full_h * 60), 60)
            cap_parts.append(f'TTF {h}h{m:02d}m')
        if cap_parts:
            body += f'<div class="temps">{" &nbsp;·&nbsp; ".join(cap_parts)}</div>'

        # ── Pack info row ─────────────────────────────────────────────────────
        info_parts = []
        if r.cell_count:
            info_parts.append(f'{r.cell_count} cells')
        if r.cycle_count is not None:
            info_parts.append(f'{r.cycle_count} cycles')
        if r.charge_fet is not None:
            cfet = '✓' if r.charge_fet else '✗'
            dfet = '✓' if r.discharge_fet else '✗'
            info_parts.append(f'CHG {cfet} DSG {dfet}')
        if r.sw_version:
            info_parts.append(f'fw {_esc(r.sw_version)}')
        if info_parts:
            body += f'<div class="temps">{" &nbsp;·&nbsp; ".join(info_parts)}</div>'

        # ── Temperatures ──────────────────────────────────────────────────────
        if r.temp_c:
            body += (f'<div class="temps">NTC: '
                     f'{"  ".join(f"{t}°C" for t in r.temp_c)}</div>')

        # ── Faults (only if active) ───────────────────────────────────────────
        if r.faults:
            fault_str = _esc(', '.join(r.faults))
            body += (f'<div class="temps" style="color:var(--red)">'
                     f'⚠ {fault_str}</div>')

        # ── Balance activity (only when any cell is balancing) ────────────────
        if r.balance_cells and any(r.balance_cells):
            bal_cells = [str(i+1) for i, b in enumerate(r.balance_cells) if b]
            body += (f'<div class="temps" style="color:var(--yellow,#f59e0b)">'
                     f'⚡ Balancing cell{"s" if len(bal_cells)>1 else ""}: '
                     f'{", ".join(bal_cells)}</div>')

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
              f'<div class="card-name">{_esc(r.name)}</div>'
              f'<div class="card-addr">{_esc(r.address)}</div></div>'
              f'<div class="badge {badge}">{badge_label}</div></div>')

    if r.error and not partial:
        body = f'<div class="error-msg">⚠ {_esc(r.error)}</div>'
    else:
        err_note = f'<div class="error-msg" style="margin-top:8px">⚠ {_esc(r.error)}</div>' if r.error else ""

        if r.device_type == "inverter":
            # Complete alarm bitmask per Victron spec (used for 0x03/0x07 records)
            _ALARM_BITS = {
                0: "Low Batt V", 1: "High Batt V", 2: "Low SOC",
                3: "Low Starter V", 4: "High Starter V", 5: "Low Temp",
                6: "High Temp", 7: "Mid Voltage", 8: "Overload",
                9: "DC Ripple", 10: "Low AC Out V", 11: "High AC Out V",
                12: "Short Circuit", 13: "BMS Lockout",
            }

            state_str = _esc(r.inverter_state) if r.inverter_state else "—"

            # ── VE.Bus Smart Dongle - layout matches VictronConnect labels ────
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
                alarm_str  = _esc(r.alarm_reason) if r.alarm_reason else "None"
                alarm_color = "var(--red)" if r.alarm_reason else "var(--green)"
                ac_in_src  = _esc(r.ac_in_source) if r.ac_in_source else "—"

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
                    f'<div class="state-kv"><span class="state-k">STATE</span><span class="state-v">{_esc(r.charger_state) if r.charger_state else "—"}</span></div>'
                    f'<div class="state-kv"><span class="state-k">YIELD</span><span class="state-v">{_fmt(r.yield_today_wh,0)} Wh</span></div>'
                    + (f'<div class="state-kv"><span class="state-k">LOAD</span><span class="state-v">{load_str}</span></div>' if load_str else '')
                    + (f'<div class="state-kv"><span class="state-k">ERR</span><span class="state-v">{r.error_code}</span></div>' if r.error_code else '')
                    + f'</div>{err_note}')

    return f'<div class="card mppt">{header}{body}</div>'


def _no_card(msg: str) -> str:
    return f'<div class="no-card">{_esc(msg)}</div>'


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate cards - one per system category, shown at top of dashboard
# ─────────────────────────────────────────────────────────────────────────────

def render_mppt_aggregate_card(mppt_readings: list) -> str:
    """MPPT aggregate - total PV power in, yield today, charger states."""
    ok = [r for r in mppt_readings
          if not r.error and r.device_type == "mppt"]

    total_pv    = sum(r.pv_power_w    or 0 for r in ok)
    total_yield = sum(r.yield_today_wh or 0 for r in ok)
    online      = len(ok)
    total       = len([r for r in mppt_readings if r.device_type == "mppt"])

    states: dict[str, int] = {}
    for r in ok:
        s = r.charger_state or "Unknown"
        states[s] = states.get(s, 0) + 1
    state_str = "  ·  ".join(f"{n}× {_esc(s)}" for s, n in sorted(states.items())) or "—"

    pv_str = _fmt(total_pv, 1)
    yld_str = _fmt(total_yield, 0)

    body = (
        f'<div class="agg-title">☀ Solar — MPPT Chargers</div>'
        f'<div class="agg-big" style="color:var(--solar)">{pv_str}'
        f'<span class="agg-big-unit"> W</span></div>'
        f'<div class="agg-summary">'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val" style="color:var(--violet)">{yld_str}</div>'
        f'<div class="agg-stat-lbl">Yield Today (Wh)</div>'
        f'</div>'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val">{online}/{total}</div>'
        f'<div class="agg-stat-lbl">Chargers online</div>'
        f'</div>'
        f'<div class="agg-stat" style="flex:1;min-width:120px">'
        f'<div class="agg-stat-val" style="font-size:1rem;color:var(--muted)">{state_str}</div>'
        f'<div class="agg-stat-lbl">States</div>'
        f'</div>'
        f'</div>'
    )
    return f'<div class="card agg-mppt">{body}</div>'


def render_inverter_aggregate_card(mppt_readings: list) -> str:
    """Inverter aggregate - total AC out, states, alarms."""
    ok = [r for r in mppt_readings
          if not r.error and r.device_type == "inverter"]

    total_ac_out = sum(r.ac_out_power_va or 0 for r in ok)
    online       = len(ok)
    total        = len([r for r in mppt_readings if r.device_type == "inverter"])

    states: dict[str, int] = {}
    for r in ok:
        s = r.inverter_state or "Unknown"
        states[s] = states.get(s, 0) + 1
    state_str = "  ·  ".join(f"{n}× {_esc(s)}" for s, n in sorted(states.items())) or "—"

    alarms = [r for r in ok if r.alarm_reason and r.alarm_reason not in (None, 0, "None")]
    alarm_str   = f"{len(alarms)} alarm(s)" if alarms else "None"
    alarm_color = "var(--red)" if alarms else "var(--green)"

    ac_str = _fmt(total_ac_out, 0)

    body = (
        f'<div class="agg-title">⚡ Inverter / VE.Bus</div>'
        f'<div class="agg-big" style="color:var(--violet)">{ac_str}'
        f'<span class="agg-big-unit"> W AC out</span></div>'
        f'<div class="agg-summary">'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val">{online}/{total}</div>'
        f'<div class="agg-stat-lbl">Inverters online</div>'
        f'</div>'
        f'<div class="agg-stat" style="flex:1;min-width:120px">'
        f'<div class="agg-stat-val" style="font-size:1rem;color:var(--muted)">{state_str}</div>'
        f'<div class="agg-stat-lbl">States</div>'
        f'</div>'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val" style="color:{alarm_color}">{alarm_str}</div>'
        f'<div class="agg-stat-lbl">Alarms</div>'
        f'</div>'
        f'</div>'
    )
    return f'<div class="card agg-inv">{body}</div>'


def render_battery_aggregate_card(bms_readings: list) -> str:
    """Battery aggregate - avg SoC bar, total Wh remaining, total Ah, packs online."""
    ok = [r for r in bms_readings if not r.error and r.capacity_pct is not None]
    total  = len(bms_readings)
    online = len(ok)

    avg_soc     = round(sum(r.capacity_pct for r in ok) / online) if ok else None
    total_wh    = sum(r.remain_wh   or 0 for r in ok)
    total_ah    = sum(r.remain_ah   or 0 for r in ok)
    nom_wh      = sum(r.nominal_wh  or 0 for r in ok)
    net_amps    = sum(r.current_a   or 0 for r in ok if r.current_a is not None)

    color   = _soc_color(avg_soc) if avg_soc is not None else "var(--muted)"
    pct_str = f"{avg_soc}%" if avg_soc is not None else "—"
    wh_str  = _fmt(total_wh, 0)
    ah_str  = f"{total_ah:.1f}"
    nom_str = f"{nom_wh:.0f}" if nom_wh else "—"

    bar = (
        f'<div style="margin:14px 0 6px">'
        f'<div class="soc-track" style="height:10px;border-radius:5px">'
        f'<div class="soc-fill" style="width:{avg_soc or 0}%;background:{color};'
        f'border-radius:5px;transition:width 1s ease"></div>'
        f'</div></div>'
    )

    body = (
        f'<div class="agg-title">🔋 Battery Bank</div>'
        f'<div class="agg-big" style="color:{color}">{pct_str}'
        f'<span class="agg-big-unit"> avg SoC</span></div>'
        f'{bar}'
        f'<div class="agg-summary">'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val" style="color:var(--accent)">{wh_str}</div>'
        f'<div class="agg-stat-lbl">Wh remaining</div>'
        f'</div>'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val">{ah_str} / {nom_str}</div>'
        f'<div class="agg-stat-lbl">Ah rem / nom</div>'
        f'</div>'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val" style="color:var(--green)">{_fmt(net_amps, 1)}</div>'
        f'<div class="agg-stat-lbl">Net amps</div>'
        f'</div>'
        f'<div class="agg-stat">'
        f'<div class="agg-stat-val">{online}/{total}</div>'
        f'<div class="agg-stat-lbl">Packs online</div>'
        f'</div>'
        f'</div>'
    )
    return f'<div class="card agg-bat">{body}</div>'


def build_html(bms_readings: list, mppt_readings: list, history: dict, theme: str = "dark") -> str:
    """
    Render the complete HTML dashboard as a string.

    Layout (top to bottom):
      1. MPPT aggregate card  - total PV W, yield today, charger states
      2. Inverter aggregate card - total AC out W, states, alarms
      3. Battery aggregate card  - avg SoC bar, total Wh/Ah, net amps, pack count
      4. Individual MPPT charger cards
      5. Individual Inverter cards
      6. Individual BMS pack cards
      7. Historical charts

    Args:
        bms_readings:  DeviceReading list from JBD/Vatrer BMS devices.
        mppt_readings: DeviceReading list from Victron devices (all types).
        history:       Rolling dict of {device_name: [reading_dict, …]} for charts.
        theme:         CSS theme class applied to <body>: "dark", "light", or "business".
    """
    # Split Victron readings by device type
    mppt_solar = [r for r in mppt_readings if r.device_type == "mppt"]
    mppt_inv   = [r for r in mppt_readings if r.device_type == "inverter"]
    mppt_other = [r for r in mppt_readings
                  if r.device_type not in ("mppt", "inverter")]

    # Aggregate cards
    mppt_agg = render_mppt_aggregate_card(mppt_readings) if mppt_readings else \
               _no_card("No MPPT chargers configured")
    inv_agg  = render_inverter_aggregate_card(mppt_readings) if mppt_inv else \
               _no_card("No inverters configured")
    bat_agg  = render_battery_aggregate_card(bms_readings) if bms_readings else \
               _no_card("No BMS packs configured")

    # Individual device cards, grouped by type
    mppt_cards = (
        "".join(render_victron_card(r) for r in mppt_solar + mppt_other)
        or _no_card("No MPPT chargers found")
    )
    inv_cards  = (
        "".join(render_victron_card(r) for r in mppt_inv)
        or _no_card("No inverters found")
    )
    bms_cards  = (
        "".join(render_bms_card(r) for r in bms_readings)
        or _no_card("No JBD / Vatrer BMS devices found")
    )

    return (HTML_TEMPLATE
            .replace("__TIMESTAMP__",    datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
            .replace("__BMS_COUNT__",    str(len(bms_readings)))
            .replace("__MPPT_COUNT__",   str(len(mppt_readings)))
            .replace("__MPPT_AGG__",     mppt_agg)
            .replace("__INV_AGG__",      inv_agg)
            .replace("__BAT_AGG__",      bat_agg)
            .replace("__MPPT_CARDS__",   mppt_cards)
            .replace("__INV_CARDS__",    inv_cards)
            .replace("__BMS_CARDS__",    bms_cards)
            .replace("__SERVER_THEME__", theme)
            # "</" must not appear inside a <script> block: a device name
            # containing "</script>" would otherwise terminate the tag and
            # inject markup.  JSON semantics are unchanged by the escape.
            .replace("__HISTORY_JSON__",
                     json.dumps(history, indent=2).replace("</", "<\\/"))
            # Inject last so placeholder replacement never scans the JS bundle
            .replace("__CHARTJS__", _chartjs()))


