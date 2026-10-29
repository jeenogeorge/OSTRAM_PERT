"""A self-contained HTML dashboard of how the run perturbed its inputs.

The charts are drawn in the browser with plotly.js. If the Python ``plotly``
package is installed its copy of plotly.js is embedded, so the file works
offline; otherwise the page loads plotly.js from the CDN.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .levers import applies, feasibility_relevant
from .perturbed import Perturbation
from .run import KEY_COLUMNS, STATUS_HELP, SOLVED, RunData

CDN = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"


def _plotly_script() -> str:
    try:
        from plotly.offline import get_plotlyjs
        return f"<script>{get_plotlyjs()}</script>"
    except Exception:
        return f'<script src="{CDN}"></script>'


def _clean(values) -> list:
    return [None if (v is None or not np.isfinite(v)) else round(float(v), 8) for v in values]


def _lever_payload(data: RunData, p: Perturbation, lever) -> dict | None:
    rows = p.rows.get(lever.lever_id)
    info = p.levers.loc[p.levers.Lever == lever.lever_id].iloc[0].to_dict()
    checks = p.checks[p.checks.Lever == lever.lever_id]
    base = {
        "id": lever.lever_id, "kind": lever.kind, "name": lever.name or "",
        "parameter": lever.parameter, "describe": lever.describe(),
        "applies_to": info.get("Applies_To", ""),
        "members": info.get("Members", ""), "n_members": int(info.get("Members_Matched", 0) or 0),
        "feasibility": feasibility_relevant(lever.parameter),
        "checks": [f"{r.Future_ID + ': ' if r.Future_ID else ''}{r.Check}"
                   + (f" ({r.Detail})" if r.Detail else "") for r in checks.itertuples()],
        "unsolved_applied": [f for f in data.unsolved if applies(data, lever, f)],
    }
    if rows is None or rows.empty:
        base.update(years=[], series=[], rep="", varies=False, band=None)
        return base
    keys = [c for c in KEY_COLUMNS if rows[c].astype(str).ne("").any()]
    piv = rows.pivot_table(index=keys + ["YEAR"], columns="Future_ID", values="Value", aggfunc="first")
    years = sorted(int(y) for y in piv.index.get_level_values("YEAR").unique())
    # representative row: the member with the widest spread across futures in the late years
    late = [y for y in years if y >= years[-1] - 5]
    spread = piv[piv.index.get_level_values("YEAR").isin(late)]
    spread = (spread.max(axis=1) - spread.min(axis=1)).groupby(level=keys).sum()
    rep_key = spread.idxmax() if len(spread) and spread.max() > 0 else (spread.index[0] if len(spread) else None)
    rep_key = rep_key if isinstance(rep_key, tuple) else (rep_key,)
    rep = piv.xs(rep_key, level=keys) if len(keys) else piv
    rep_label = " · ".join(str(k) for k in rep_key if str(k) != "")
    ref = p.reference
    ratio = piv.div(piv[ref].replace(0, np.nan), axis=0) if ref in piv.columns else None
    med = ratio.groupby(level="YEAR").median() if ratio is not None else None

    series = []
    for f in data.futures:
        if f not in piv.columns:
            continue
        series.append({
            "f": f, "status": data.status_of(f), "applies": bool(applies(data, lever, f)),
            "ref": f == ref,
            "abs": _clean(rep[f].reindex(years).values),
            "ratio": _clean(med[f].reindex(years).values) if med is not None and f in med.columns else [],
        })
    band = None
    if lever.kind == "Uncertainty" and lever.method in ("Multiply", "Multiply_Ramp") \
            and lever.minimum is not None and lever.maximum is not None:
        band = [lever.minimum, lever.maximum]
    varies = bool(np.nanmax(np.abs(piv.max(axis=1) - piv.min(axis=1)).values) > 1e-12) if len(piv) else False
    base.update(years=years, series=series, rep=rep_label, varies=varies, band=band)
    return base


def _heatmap_payload(data: RunData, p: Perturbation) -> dict:
    unc = [lv for lv in data.interface.levers if lv.kind == "Uncertainty"]
    sampled = [f for f in data.futures if data.sample(f) > 0]
    order = sorted(sampled, key=lambda f: (data.status_of(f) == SOLVED, data.futures.index(f)))
    bf = p.by_future.set_index(["Lever", "Future_ID"]) if len(p.by_future) else None
    z, text = [], []
    for lv in unc:
        zr, tr = [], []
        for f in order:
            pos = drawn = np.nan
            if bf is not None and (lv.lever_id, f) in bf.index:
                pos = bf.loc[(lv.lever_id, f), "Position_in_Range"]
                drawn = bf.loc[(lv.lever_id, f), "Drawn"]
            zr.append(None if not np.isfinite(pos) else round(float(pos), 4))
            tr.append("" if not np.isfinite(drawn) else f"{drawn:.4g}")
        z.append(zr)
        text.append(tr)
    return {
        "x": [f + (" ✕" if data.status_of(f) != SOLVED else "") for f in order],
        "y": [f"{lv.lever_id} · {lv.parameter}" for lv in unc],
        "z": z, "text": text,
    }


MAX_SET_ROWS = 4000   # rows of the "Inputs by set" explorer kept in the page


def _sets_payload(data: RunData, listing) -> dict | None:
    """Year-wise values of every input row that differs between futures, grouped by set family."""
    from .allsets import FAMILIES, NO_YEAR
    if listing is None or listing.long is None or listing.long.empty:
        return None
    lg = listing.long.copy()
    lg["YEAR"] = lg["YEAR"].map(lambda y: NO_YEAR if pd.isna(y) else int(y))
    keys = ["Parameter"] + KEY_COLUMNS
    years = [y for y in listing.years]
    w = lg.pivot_table(index=keys + ["Future_ID"], columns="YEAR", values="Value", aggfunc="first")
    w = w.reindex(columns=[y for y in years if y in w.columns])
    years = list(w.columns)
    # rank rows by how far they move from the reference, keep the strongest
    ref = listing.reference
    rank_col = f"Max_Rel_Change_vs_{ref}"
    rank = listing.varying.groupby(keys)[rank_col].max() if rank_col in listing.varying else None
    row_keys = list(dict.fromkeys(w.index.droplevel("Future_ID")))
    note = ""
    if len(row_keys) > MAX_SET_ROWS and rank is not None:
        row_keys = list(rank.reindex(row_keys).fillna(0).sort_values(ascending=False).index[:MAX_SET_ROWS])
        note = (f"Showing the {MAX_SET_ROWS} rows that move most; every row is in the "
                f"Inputs_by_Set workbook.")
    multi_region = len({k[keys.index("REGION")] for k in row_keys}) > 1
    rows = []
    for k in row_keys:
        sub = w.xs(k, level=keys, drop_level=True)
        fam = [name for name, col in FAMILIES if col and k[keys.index(col)] != ""] or ["Region"]
        shown = [c for c in KEY_COLUMNS if c != "REGION" or multi_region]
        label = " · ".join(str(k[keys.index(c)]) for c in shown if k[keys.index(c)] != "")
        rows.append({"p": k[0], "k": label or str(k[keys.index("REGION")]) or "(all)", "fam": fam,
                     "s": {f: _clean(sub.loc[f].values) for f in sub.index}})
    return {"years": [str(y) for y in years], "rows": rows, "note": note,
            "families": [n for n, _ in FAMILIES]}


def _diagnosis_rows(dx) -> str:
    if dx is None:
        return ""
    if dx.summary is None or dx.summary.empty:
        return "<tr><td colspan='4'>Every future solved: nothing to diagnose.</td></tr>"
    out = []
    for r in dx.summary.itertuples():
        cls = "bad" if "Likely" in r.Verdict else ("warn" if "Possible" in r.Verdict else "")
        leads = "".join(f"<li>{html.escape(x.lstrip('• '))}</li>" for x in str(r.Leads).splitlines())
        out.append(f"<tr class='{cls}'><td><b>{html.escape(r.Future_ID)}</b></td>"
                   f"<td><span class='pill'>{html.escape(r.Solver_Status)}</span></td>"
                   f"<td><b>{html.escape(r.Verdict)}</b></td><td><ul class='leads'>{leads}</ul></td></tr>")
    return "".join(out)


def build_dashboard(data: RunData, p: Perturbation, path: Path, listing=None, dx=None) -> Path:
    levers = [_lever_payload(data, p, lv) for lv in data.interface.levers]
    st = data.status
    status_rows = "".join(
        f"<tr class='{'bad' if r.Solver_Status != SOLVED else ''}'><td>{html.escape(r.Future_ID)}</td>"
        f"<td>{html.escape(str(r.Named_Future or data.named(r.Future_ID)))}</td>"
        f"<td>{html.escape(str(data.sample(r.Future_ID)))}</td>"
        f"<td><span class='pill {html.escape(r.Solver_Status)}'>{html.escape(r.Solver_Status)}</span></td>"
        f"<td class='mono'>{html.escape(r.Solver_Message or '')}</td></tr>"
        for r in st.itertuples())
    checks_rows = "".join(
        f"<tr><td><a href='#L-{html.escape(r.Lever)}'>{html.escape(r.Lever)}</a></td><td>{html.escape(r.Parameter)}</td>"
        f"<td>{html.escape(r.Future_ID)}</td><td>{html.escape(r.Check)}</td><td>{html.escape(r.Detail)}</td></tr>"
        for r in p.checks.itertuples()) or "<tr><td colspan='5'>Nothing looks off.</td></tr>"
    n_un = len(data.unsolved)
    sets = _sets_payload(data, listing)
    status_of = {f: data.status_of(f) for f in data.futures}
    payload = {
        "run": data.ref.label, "reference": p.reference, "levers": levers,
        "heat": _heatmap_payload(data, p), "help": STATUS_HELP,
        "sets": sets, "status": status_of,
    }
    dx_rows = _diagnosis_rows(dx)
    dx_card = ("" if dx is None else
               "<div class='card' id='diag'><h2>Why futures did not solve</h2>"
               "<div class='muted'>Each unsolved future set against the solved ones: lever draws, input "
               "rows of every set, and bounds that cannot both hold. Leads, strongest first; the full "
               "tables are in Infeasibility_Diagnosis.xlsx.</div>"
               f"<div class='tbl'><table><tr><th>Future</th><th>Status</th><th>Verdict</th><th>Leads</th></tr>"
               f"{dx_rows}</table></div></div>")
    warn = "".join(f"<li>{html.escape(w)}</li>" for w in data.warnings)
    page = TEMPLATE.format(
        title=html.escape(f"RDM inputs · {data.ref.label}"),
        run=html.escape(data.ref.label), folder=html.escape(str(data.ref.path)),
        n_fut=len(data.futures), n_ok=len(data.solved), n_un=n_un, kpi_bad="bad" if n_un else "",
        n_lev=len(levers), ref=html.escape(p.reference),
        unsolved=html.escape(", ".join(data.unsolved) or "none"),
        status_rows=status_rows, checks_rows=checks_rows,
        n_checks=len(p.checks), warn=f"<ul class='warn'>{warn}</ul>" if warn else "",
        dx_card=dx_card, dx_nav="<a href='#diag'>Why futures did not solve</a>" if dx is not None else "",
        sets_nav="<a href='#sets'>Inputs by set</a>" if sets else "",
        plotly=_plotly_script(),
        payload=json.dumps(payload, allow_nan=False, default=_json_default).replace("</", "<\\/"),
    )
    path.write_text(page, encoding="utf-8")
    return path


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#f5f6f3;--panel:#fff;--fg:#1b2420;--muted:#5d6a63;--line:#dde2de;--accent:#1f6f5c;
--ok:#3d8a5f;--bad:#c0392b;--ref:#6b5ca5;--off:#b9c0bb;--warn:#b7791f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111513;--panel:#1a201d;--fg:#e3e8e5;--muted:#98a39d;
--line:#2d3531;--accent:#5fbf9f;--ok:#5fb383;--bad:#ef6b5b;--ref:#a99be0;--off:#4a524e;--warn:#e0a84a}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}}
.layout{{display:grid;grid-template-columns:270px minmax(0,1fr);min-height:100vh}}
nav{{position:sticky;top:0;height:100vh;overflow:auto;padding:16px;border-right:1px solid var(--line);background:var(--panel)}}
nav h3{{margin:14px 0 6px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}
nav label input{{margin:0 6px 0 0;vertical-align:-2px}}
nav #q{{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}}
nav a{{display:block;padding:3px 6px;border-radius:4px;color:var(--fg);text-decoration:none;font-size:12.5px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}} nav a:hover{{background:var(--bg)}}
nav a.flag{{color:var(--bad);font-weight:600}} nav label{{font-size:12px;color:var(--muted);display:block;margin-top:8px}}
main{{padding:24px;display:grid;gap:18px;align-content:start;min-width:0}}
h1{{font-size:22px;margin:0}} h2{{font-size:15px;margin:0 0 4px}} .muted{{color:var(--muted)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.kpi{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}}
.kpi b{{display:block;font-size:22px;font-variant-numeric:tabular-nums}} .kpi.bad b{{color:var(--bad)}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;min-width:0}}
.card.hidden{{display:none}}
.meta{{font:12px/1.5 Consolas,ui-monospace,monospace;color:var(--muted);margin:2px 0 6px}}
.badge{{display:inline-block;font-size:11px;padding:0 7px;border-radius:10px;border:1px solid currentColor;margin-left:6px;font-weight:500}}
.b-bad{{color:var(--bad)}} .b-warn{{color:var(--warn)}} .b-off{{color:var(--muted)}}
.tbl{{overflow-x:auto;max-height:420px}} table{{border-collapse:collapse;width:100%;font-size:12.5px}}
th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--muted);font-weight:500;position:sticky;top:0;background:var(--panel)}}
tr.bad td{{background:color-mix(in srgb,var(--bad) 10%,transparent)}}
.pill{{font-size:11px;padding:1px 8px;border-radius:10px;background:var(--bg);border:1px solid var(--line)}}
.pill.solved{{color:var(--ok)}} .pill:not(.solved){{color:var(--bad);font-weight:600}}
.mono{{font-family:Consolas,ui-monospace,monospace;font-size:11.5px}}
.charts{{display:grid;grid-template-columns:1fr 1fr;gap:10px}} .chart{{height:300px;min-width:0}}
.checks{{margin:6px 0 0;padding-left:18px;color:var(--warn);font-size:12.5px}}
.legend span{{display:inline-flex;align-items:center;gap:6px;margin-right:16px;font-size:12.5px}}
.legend i{{width:18px;height:3px;display:inline-block;border-radius:2px}}
ul.warn{{color:var(--warn);margin:4px 0 0;padding-left:18px}}
tr.warn td{{background:color-mix(in srgb,var(--warn) 10%,transparent)}}
ul.leads{{margin:0;padding-left:16px}} ul.leads li{{margin:1px 0}}
.pickers{{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0}} .pickers label{{font-size:12.5px;color:var(--muted)}}
.pickers select{{margin-left:6px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);max-width:340px}}
@media (max-width:900px){{.layout{{grid-template-columns:1fr}} nav{{position:static;height:auto;max-height:45vh}}
.charts{{grid-template-columns:1fr}}}}
</style>{plotly}</head><body><div class="layout">
<nav>
 <input id="q" placeholder="Filter levers" oninput="filt()">
 <label><input type="checkbox" id="onlyflag" onchange="filt()"> only flagged levers</label>
 <label><input type="checkbox" id="onlyfeas" onchange="filt()"> only levers that can affect feasibility</label>
 <h3>Overview</h3><a href="#status">Run status</a>{dx_nav}<a href="#heat">Draws by future</a>{sets_nav}<a href="#checks">Things that look off</a>
 <h3>Levers</h3><div id="navl"></div>
</nav>
<main>
 <div><h1>How the inputs were perturbed</h1>
 <div class="muted">Run <b>{run}</b> · {folder}</div>{warn}</div>
 <div class="kpis">
  <div class="kpi"><span class="muted">Futures</span><b>{n_fut}</b></div>
  <div class="kpi"><span class="muted">Solved</span><b>{n_ok}</b></div>
  <div class="kpi {kpi_bad}"><span class="muted">Not solved</span><b>{n_un}</b></div>
  <div class="kpi"><span class="muted">Levers</span><b>{n_lev}</b></div>
  <div class="kpi"><span class="muted">Reference future</span><b>{ref}</b></div>
 </div>
 <div class="legend"><span><i style="background:var(--ref)"></i>reference future (dotted)</span>
  <span><i style="background:var(--ok)"></i>solved</span><span><i style="background:var(--bad)"></i>not solved</span>
  <span><i style="background:var(--off)"></i>lever not applied in that future</span></div>
 <div class="card" id="status"><h2>Run status</h2><div class="muted">Not solved: {unsolved}</div>
  <div class="tbl"><table><tr><th>Future</th><th>Named future</th><th>Sample</th><th>Status</th><th>Solver says</th></tr>{status_rows}</table></div></div>
 {dx_card}
 <div class="card" id="heat"><h2>Draws by future</h2>
  <div class="muted">Where each uncertainty was drawn in its Min–Max range (0 = Min, 1 = Max). Unsolved futures (✕) come first.</div>
  <div id="heatplot" style="height:340px"></div></div>
 <div class="card" id="sets"><h2>Inputs by set, every year</h2>
  <div class="muted">Every input row that differs between futures, by the set it is keyed on. A row keyed on two sets
   (InputActivityRatio: technology and commodity) is under both.</div>
  <div class="pickers"><label>Set <select id="sfam"></select></label><label>Parameter <select id="spar"></select></label>
   <label>Row <select id="srow"></select></label></div>
  <div id="snote" class="muted"></div><div id="splot" style="height:340px"></div></div>
 <div class="card" id="checks"><h2>Things that look off ({n_checks})</h2>
  <div class="tbl"><table><tr><th>Lever</th><th>Parameter</th><th>Future</th><th>Check</th><th>Detail</th></tr>{checks_rows}</table></div></div>
 <div id="cards"></div>
</main></div>
<script>
const D = {payload};
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function colour(s){{ if(s.ref) return css('--ref'); if(!s.applies) return css('--off');
  return s.status==='solved' ? css('--ok') : css('--bad'); }}
function esc(t){{ return String(t).replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]); }}
function layout(title){{ return {{title:{{text:title,font:{{size:12}}}},margin:{{l:56,r:10,t:30,b:30}},
  paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',showlegend:false,hovermode:'closest',
  font:{{color:css('--fg'),size:11}},xaxis:{{gridcolor:css('--line')}},yaxis:{{gridcolor:css('--line'),zerolinecolor:css('--line')}}}}; }}
function traces(L, key){{
  const out=[]; const order=[...L.series].sort((a,b)=>(a.applies-b.applies)||((a.status!=='solved')-(b.status!=='solved')));
  for(const s of order){{ if(!s[key] || !s[key].length) continue;
    const bad = s.applies && s.status!=='solved';
    out.push({{x:L.years,y:s[key],mode:'lines',name:s.f,line:{{color:colour(s),width:bad?3:(s.ref?2:1.4),dash:s.ref?'dot':'solid'}},
      hovertemplate:`${{s.f}} (${{s.status}}${{s.applies?'':', not applied'}})<br>%{{x}}: %{{y:.5g}}<extra></extra>`}}); }}
  return out; }}
const cards=document.getElementById('cards'), nav=document.getElementById('navl'), drawn=new Set();
for(const L of D.levers){{
  const flag = L.checks.length>0 || L.unsolved_applied.length>0;
  const badges = (L.kind==='Change'?'<span class="badge b-off">fixed change</span>':'')
    + (!L.feasibility?'<span class="badge b-off">cost only</span>':'')
    + (L.series.length && !L.varies?'<span class="badge b-warn">no variation</span>':'')
    + (L.checks.length?'<span class="badge b-warn">check</span>':'')
    + (L.unsolved_applied.length?`<span class="badge b-bad">applied in ${{L.unsolved_applied.length}} unsolved</span>`:'');
  const c=document.createElement('div'); c.className='card'; c.id='L-'+L.id;
  c.dataset.flag=flag?1:0; c.dataset.feas=L.feasibility?1:0; c.dataset.text=(L.id+' '+L.name+' '+L.parameter+' '+L.members).toLowerCase();
  c.innerHTML = `<h2>${{esc(L.id)}} · ${{esc(L.name||L.parameter)}}${{badges}}</h2>
   <div class="meta">${{esc(L.describe)}} · applies to ${{esc(L.applies_to)}} · ${{L.n_members}} member(s): ${{esc(L.members)}}</div>
   ${{L.series.length ? `<div class="charts"><div class="chart" id="a-${{esc(L.id)}}"></div><div class="chart" id="r-${{esc(L.id)}}"></div></div>`
     : '<div class="muted">No input rows found for this lever.</div>'}}
   ${{L.checks.length?'<ul class="checks">'+L.checks.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':''}}`;
  cards.appendChild(c);
  const a=document.createElement('a'); a.href='#L-'+L.id; a.textContent=L.id+' · '+(L.name||L.parameter);
  a.dataset.id=L.id; if(flag) a.className='flag'; nav.appendChild(a);
}}
function draw(L){{
  if(drawn.has(L.id) || !L.series.length) return; drawn.add(L.id);
  Plotly.newPlot('a-'+L.id, traces(L,'abs'), layout(`${{L.parameter}} · ${{L.rep||'single row'}} (absolute)`), {{displayModeBar:false,responsive:true}});
  const lr = layout(`Median ratio to ${{D.reference}}, all members`);
  if(L.band) lr.shapes=[{{type:'rect',xref:'paper',x0:0,x1:1,y0:L.band[0],y1:L.band[1],fillcolor:css('--muted'),opacity:.12,line:{{width:0}}}}];
  Plotly.newPlot('r-'+L.id, traces(L,'ratio'), lr, {{displayModeBar:false,responsive:true}});
}}
const io = new IntersectionObserver(es=>es.forEach(e=>{{ if(e.isIntersecting){{
  draw(D.levers.find(l=>'L-'+l.id===e.target.id)); io.unobserve(e.target); }} }}), {{rootMargin:'400px'}});
document.querySelectorAll('#cards .card').forEach(c=>io.observe(c));
function filt(){{ const q=document.getElementById('q').value.toLowerCase(), f=document.getElementById('onlyflag').checked,
  fe=document.getElementById('onlyfeas').checked;
  document.querySelectorAll('#cards .card').forEach(c=>{{ const show=c.dataset.text.includes(q)&&(!f||c.dataset.flag==='1')&&(!fe||c.dataset.feas==='1');
    c.classList.toggle('hidden',!show); const a=nav.querySelector(`a[data-id="${{CSS.escape(c.id.slice(2))}}"]`); if(a) a.style.display=show?'':'none'; }}); }}
const H=D.heat;
if(H.y.length && H.x.length){{
  Plotly.newPlot('heatplot',[{{type:'heatmap',x:H.x,y:H.y,z:H.z,text:H.text,zmin:0,zmax:1,colorscale:'RdBu',reversescale:true,
    xgap:1,ygap:1,colorbar:{{title:{{text:'position'}},thickness:10}},
    hovertemplate:'%{{y}}<br>%{{x}}<br>drawn %{{text}} · position %{{z:.2f}}<extra></extra>'}}],
    Object.assign(layout(''),{{margin:{{l:200,r:10,t:10,b:80}},yaxis:{{autorange:'reversed',automargin:true}}}}),{{displayModeBar:false,responsive:true}});
  document.getElementById('heatplot').style.height=Math.max(220,40+28*H.y.length)+'px'; Plotly.Plots.resize('heatplot');
}} else document.getElementById('heatplot').outerHTML='<div class="muted">No sampled uncertainties in this run.</div>';
const S=D.sets;
if(!S){{ document.getElementById('sets').style.display='none'; }}
else {{
  const fam=document.getElementById('sfam'), par=document.getElementById('spar'), row=document.getElementById('srow');
  document.getElementById('snote').textContent=S.note||'';
  const opts=(el,vals)=>{{ el.innerHTML=vals.map(v=>`<option value="${{esc(v[0])}}">${{esc(v[1])}}</option>`).join(''); }};
  const famCount=f=>S.rows.filter(r=>r.fam.includes(f)).length;
  opts(fam, S.families.map(f=>[f, `${{f}} (${{famCount(f)}})`]));
  const firstFam=S.families.find(f=>famCount(f)>0); if(firstFam) fam.value=firstFam;
  function fillPar(){{ const ps=[...new Set(S.rows.filter(r=>r.fam.includes(fam.value)).map(r=>r.p))].sort();
    opts(par, ps.map(p=>[p,p])); fillRow(); }}
  function fillRow(){{ const rs=S.rows.map((r,i)=>[i,r]).filter(([i,r])=>r.fam.includes(fam.value)&&r.p===par.value);
    opts(row, rs.map(([i,r])=>[String(i), r.k])); plotRow(); }}
  function plotRow(){{
    const r=S.rows[+row.value]; if(!r){{ Plotly.purge('splot'); return; }}
    const tr=Object.entries(r.s).map(([f,y])=>{{ const st=D.status[f]||'unknown', ref=f===D.reference;
      const c=ref?css('--ref'):(st==='solved'?css('--ok'):css('--bad'));
      return {{x:S.years,y:y,mode:'lines+markers',name:f,line:{{color:c,width:st!=='solved'?3:(ref?2:1.4),dash:ref?'dot':'solid'}},
        marker:{{size:4}},hovertemplate:`${{f}} (${{st}})<br>%{{x}}: %{{y:.5g}}<extra></extra>`}}; }});
    tr.sort((a,b)=>(D.status[a.name]!=='solved')-(D.status[b.name]!=='solved'));
    Plotly.newPlot('splot', tr, Object.assign(layout(`${{r.p}} · ${{r.k}}`),{{showlegend:true,legend:{{orientation:'h',y:-0.2}}}}),
      {{displayModeBar:false,responsive:true}}); }}
  fam.onchange=fillPar; par.onchange=fillRow; row.onchange=plotRow; fillPar();
}}
</script></body></html>"""
