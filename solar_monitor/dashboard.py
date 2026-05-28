"""
solar_monitor/dashboard.py — HTML dashboard rendering
======================================================
Builds the self-contained HTML dashboard file from DeviceReading data.

Layout (top to bottom):
  1. Header bar (brand, timestamp, theme toggle)
  2. Aggregate cards row — MPPT totals, Inverter totals, Battery totals
  3. Individual MPPT solar charger cards
  4. Individual Inverter cards
  5. Individual BMS pack cards
  6. Historical chart section (battery V, current, PV power, SoC)

Three switchable colour themes: dark, light, business (localStorage persistence).
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
  body.business::before { display:none; }
  body.business header {
    background:#ffffff; border-bottom:1px solid var(--border);
    box-shadow:0 1px 3px rgba(0,0,0,.06); padding:18px 32px;
  }
  body.business .brand {
    font-family:'DM Serif Display',serif; font-weight:400; font-size:1.75rem;
    letter-spacing:0; text-shadow:none; text-transform:none; color:var(--text);
  }
  body.business .brand span { color:var(--accent); }
  body.business .meta {
    font-family:'Inter',sans-serif; font-size:.75rem; color:var(--muted); line-height:1.5;
  }
  body.business .meta strong { color:var(--text); }
  body.business .section-title {
    font-family:'Inter',sans-serif; font-size:.65rem; font-weight:700;
    letter-spacing:.1em; text-transform:uppercase;
    padding:20px 32px 10px; color:var(--muted); border-bottom:1px solid var(--border);
  }
  body.business .cards {
    display:grid; grid-template-columns:repeat(auto-fill, minmax(280px,1fr));
    gap:12px; padding:16px 32px; background:none; border:none;
  }
  body.business .agg-cards {
    display:flex; flex-wrap:nowrap;
    gap:12px; padding:16px 32px; background:none; border:none;
  }
  body.business .agg-cards>.card { flex:1 1 0; min-width:0; }
  body.business .card {
    background:#fff; border:1px solid var(--border); border-radius:12px;
    padding:16px 18px; box-shadow:0 1px 4px rgba(0,0,0,.06);
    border-left:none; min-width:0; overflow:hidden;
  }
  body.business .card.bms       { border-top:3px solid var(--accent); }
  body.business .card.mppt      { border-top:3px solid var(--amber); }
  body.business .card.agg-mppt  { border-top:3px solid var(--solar);  min-width:260px; }
  body.business .card.agg-inv   { border-top:3px solid var(--violet); min-width:260px; }
  body.business .card.agg-bat   { border-top:3px solid var(--accent); min-width:260px; }
  body.business .card-header { gap:8px; align-items:flex-start; }
  body.business .card-name {
    font-family:'Inter',sans-serif; font-weight:600; font-size:.85rem;
    letter-spacing:0; text-transform:none; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis;
  }
  body.business .card-addr {
    font-family:'Inter',sans-serif; font-size:.6rem; color:var(--muted);
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }
  body.business .type-pill {
    font-family:'Inter',sans-serif; font-size:.58rem; font-weight:700;
    border-radius:4px; letter-spacing:.03em; padding:1px 6px;
  }
  body.business .type-pill.bms  { background:rgba(29,78,216,.08);  color:var(--accent); border-color:rgba(29,78,216,.25); }
  body.business .type-pill.mppt { background:rgba(180,83,9,.08);   color:var(--amber);  border-color:rgba(180,83,9,.25); }
  body.business .type-pill.inv  { background:rgba(109,40,217,.08); color:var(--violet); border-color:rgba(109,40,217,.25); }
  body.business .type-pill.mon  { background:rgba(21,128,61,.08);  color:var(--green);  border-color:rgba(21,128,61,.25); }
  body.business .badge {
    font-family:'Inter',sans-serif; font-weight:600; font-size:.58rem;
    border-radius:4px; letter-spacing:.03em; padding:2px 7px; white-space:nowrap; flex-shrink:0;
  }
  body.business .badge.ok    { background:rgba(21,128,61,.1);  color:var(--green); border-color:rgba(21,128,61,.3); }
  body.business .badge.error { background:rgba(185,28,28,.08); color:var(--red);   border-color:rgba(185,28,28,.25); }
  body.business .badge.warn  { background:rgba(180,83,9,.08);  color:var(--amber); border-color:rgba(180,83,9,.25); }
  body.business .metrics { gap:6px; margin-bottom:10px; }
  body.business .metric-val { font-family:'Inter',sans-serif; font-weight:700; font-size:1.2rem; line-height:1; }
  body.business .metric-lbl { font-family:'Inter',sans-serif; font-weight:500; font-size:.58rem; letter-spacing:.04em; }
  body.business .soc-track { background:rgba(0,0,0,.08); }
  body.business .soc-label { font-family:'Inter',sans-serif; font-size:.68rem; white-space:nowrap; }
  body.business .state-row { font-family:'Inter',sans-serif; font-size:.68rem; gap:10px; }
  body.business .state-k { font-family:'Inter',sans-serif; font-weight:500; }
  body.business .state-v { font-family:'Inter',sans-serif; }
  body.business .agg-title {
    font-family:'Inter',sans-serif; font-weight:600;
    font-size:.65rem; letter-spacing:.06em; text-transform:uppercase; margin-bottom:14px;
  }
  body.business .agg-stat-val { font-family:'Inter',sans-serif; font-weight:700; font-size:1.9rem; }
  body.business .agg-stat-lbl {
    font-family:'Inter',sans-serif; font-weight:500; font-size:.62rem;
    letter-spacing:.05em; text-transform:uppercase;
  }
  body.business .agg-big {
    font-family:'Inter',sans-serif; font-weight:700; font-size:2.8rem;
    line-height:1; letter-spacing:-.02em;
  }
  body.business .agg-big-unit { font-family:'Inter',sans-serif; font-weight:400; font-size:.8rem; color:var(--muted); }
  body.business .error-msg { font-family:'Inter',sans-serif; font-size:.72rem; }
  body.business .chart-box { background:#fff; border-color:var(--border); }
  body.business .chart-title { font-family:'Inter',sans-serif; font-weight:600; font-size:.68rem; letter-spacing:.04em; text-transform:uppercase; color:var(--muted); }
  body.business .theme-btn { font-family:'Inter',sans-serif; font-weight:500; border-radius:6px; font-size:.72rem; border-color:var(--border); }
  body.business footer { font-family:'Inter',sans-serif; }

  /* ── Base (dark / light) ─────────────────────────────────────────────────── */
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
  .section-title{padding:26px 40px 10px;font-size:.68rem;letter-spacing:.25em;
    text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border)}

  /* aggregate cards row — always 3 columns on wide screens, wraps on mobile */
  .agg-cards{display:flex;flex-wrap:nowrap;gap:1px;
    background:var(--border);border-bottom:1px solid var(--border)}
  .agg-cards>.card{flex:1 1 0;min-width:0}
  .card.agg-mppt{background:var(--panel);padding:22px 28px;border-left:3px solid var(--solar);min-width:260px}
  .card.agg-inv {background:var(--panel);padding:22px 28px;border-left:3px solid var(--violet);min-width:260px}
  .card.agg-bat {background:var(--panel);padding:22px 28px;border-left:3px solid var(--accent);min-width:260px}

  /* individual device cards */
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
    gap:1px;background:var(--border);border-bottom:1px solid var(--border)}
  .card{background:var(--panel);padding:22px 26px}
  .card.mppt{border-left:3px solid var(--solar)}
  .card.bms{border-left:3px solid var(--accent)}

  /* aggregate card internals */
  .agg-title{font-size:.65rem;letter-spacing:.2em;text-transform:uppercase;
    color:var(--muted);margin-bottom:14px}
  .agg-big{font-family:'Share Tech Mono',monospace;font-size:3.2rem;line-height:1;
    font-weight:700;letter-spacing:-.01em}
  .agg-big-unit{font-family:'Share Tech Mono',monospace;font-size:.8rem;color:var(--muted)}
  .agg-summary{display:flex;gap:28px;flex-wrap:wrap;margin-top:14px}
  .agg-stat{display:flex;flex-direction:column;gap:2px}
  .agg-stat-val{font-family:'Share Tech Mono',monospace;font-size:1.6rem;line-height:1}
  .agg-stat-lbl{font-size:.6rem;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}

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
    .agg-cards{flex-wrap:wrap}.cards{grid-template-columns:1fr}.chart-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div class="brand">Solar <span>Monitor</span></div>
  <div style="display:flex;align-items:center;gap:16px">
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
            info_parts.append(f'fw {r.sw_version}')
        if info_parts:
            body += f'<div class="temps">{" &nbsp;·&nbsp; ".join(info_parts)}</div>'

        # ── Temperatures ──────────────────────────────────────────────────────
        if r.temp_c:
            body += (f'<div class="temps">NTC: '
                     f'{"  ".join(f"{t}°C" for t in r.temp_c)}</div>')

        # ── Faults (only if active) ───────────────────────────────────────────
        if r.faults:
            fault_str = ', '.join(r.faults)
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


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate cards — one per system category, shown at top of dashboard
# ─────────────────────────────────────────────────────────────────────────────

def render_mppt_aggregate_card(mppt_readings: list) -> str:
    """MPPT aggregate — total PV power in, yield today, charger states."""
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
    state_str = "  ·  ".join(f"{n}× {s}" for s, n in sorted(states.items())) or "—"

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
    """Inverter aggregate — total AC out, states, alarms."""
    ok = [r for r in mppt_readings
          if not r.error and r.device_type == "inverter"]

    total_ac_out = sum(r.ac_out_power_va or 0 for r in ok)
    online       = len(ok)
    total        = len([r for r in mppt_readings if r.device_type == "inverter"])

    states: dict[str, int] = {}
    for r in ok:
        s = r.inverter_state or "Unknown"
        states[s] = states.get(s, 0) + 1
    state_str = "  ·  ".join(f"{n}× {s}" for s, n in sorted(states.items())) or "—"

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
    """Battery aggregate — avg SoC bar, total Wh remaining, total Ah, packs online."""
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
      1. MPPT aggregate card  — total PV W, yield today, charger states
      2. Inverter aggregate card — total AC out W, states, alarms
      3. Battery aggregate card  — avg SoC bar, total Wh/Ah, net amps, pack count
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
            .replace("__HISTORY_JSON__", json.dumps(history, indent=2)))


