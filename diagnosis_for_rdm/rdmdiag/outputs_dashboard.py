"""An interactive dashboard of a run's results (RDM_Outputs.csv), in the style of
the Botswana RDM dashboards (build_bwa_dashboard_v5.py): a sidebar of tabs in
groups, one control bar, and every figure stored per future so the page can
show the average of the sampled futures, the central future, or any single
sample, and the difference from a reference.

In OSTRAM the "strategies" are the named futures (F00, F01, ...) and the
"futures" are their samples (F01_s001, F01_s002, ...). Sample 0 is the named
future run without its sampled uncertainties (Run_Central_Future = Y).

Technologies are grouped with OSTRAM's own taxonomy, the families and colours
of ostram/reporting/training_dashboard.py (Coal, Gas, Solar PV ...), and by
country from the code (PWRSPVBGDXX -> BGD). Only the futures that solved have
results; the others are named in the header.

The tabs are built from what RDM_Outputs.csv holds: a tab whose variable was
not exported (Export = N on the Outputs sheet) says so instead of drawing.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .run import SOLVED, RunData, _norm_key

# OSTRAM technology families: label and colour, as in
# ostram/reporting/training_dashboard.py (kept here so this folder imports
# nothing from the ostram package).
TECH_FAMILIES = {
    "PWRCOA": ("Coal", "#4a4a4a"), "PWRNGS": ("Gas", "#d4a017"),
    "PWROIL": ("Oil", "#8b4513"), "PWRPET": ("Petroleum", "#a0522d"),
    "PWRURN": ("Nuclear", "#e74c3c"), "PWRHYD": ("Hydro", "#2980b9"),
    "PWRSHP": ("Small Hydro", "#5dade2"), "PWRSPV": ("Solar PV", "#f39c12"),
    "PWRWON": ("Wind Onshore", "#27ae60"), "PWRWOF": ("Wind Offshore", "#1abc9c"),
    "PWRCSP": ("CSP", "#e67e22"), "PWRBIO": ("Biomass", "#7d3c98"),
    "PWRWAS": ("Waste", "#95a5a6"), "PWRGEO": ("Geothermal", "#c0392b"),
    "PWROTH": ("Other", "#bdc3c7"), "PWRCCS": ("CCS", "#566573"),
}
ORDERED_FAMILIES = [
    "Coal", "Petroleum", "Oil", "Gas", "Nuclear", "Other", "CCS", "Waste",
    "Biomass", "Geothermal", "Hydro", "Small Hydro", "CSP", "Solar PV",
    "Wind Onshore", "Wind Offshore",
]
STORAGE = {"PWRSDS": ("Storage, short duration", "#8e44ad"),
           "PWRLDS": ("Storage, long duration", "#5b2c6f")}
BACKSTOP = ("PWRBCK", "Backstop", "#ff00aa")
VRE = ("PWRSPV", "PWRWON", "PWRWOF")
INTERCONNECTOR = re.compile(r"^TRN([A-Z]{3}[A-Z0-9]{2})([A-Z]{3}[A-Z0-9]{2})$")

COST_PARTS = [("CapitalInvestment", "Capital", "#13334f"),
              ("AnnualFixedOperatingCost", "Fixed O&M", "#6f8ea3"),
              ("AnnualVariableOperatingCost", "Variable O&M", "#2a6f97")]
PAL = ["#13334f", "#2a6f97", "#9c4a3a", "#c9a227", "#5a8f69", "#6f8ea3", "#8e6f9c",
       "#c97f4a", "#4aa0b9", "#7a4fa3", "#3d8a5f", "#b5651d"]
CDN = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"

NEEDED = ["TECHNOLOGY", "FUEL", "EMISSION", "YEAR", "ProductionByTechnologyAnnual",
          "TotalCapacityAnnual", "NewCapacity", "AnnualEmissions", "TotalDiscountedCost",
          "CapitalInvestment", "AnnualFixedOperatingCost", "AnnualVariableOperatingCost",
          "DiscountedCostByTechnology", "TotalTechnologyAnnualActivity"]


# --------------------------------------------------------------------------- #
# Classifiers
# --------------------------------------------------------------------------- #
def family(t: str) -> str | None:
    t = str(t)
    for p, (lab, _) in TECH_FAMILIES.items():
        if t.startswith(p):
            return lab
    for p, (lab, _) in STORAGE.items():
        if t.startswith(p):
            return lab
    if t.startswith(BACKSTOP[0]):
        return BACKSTOP[1]
    return None


def is_generation(t: str) -> bool:
    t = str(t)
    return t.startswith(tuple(TECH_FAMILIES)) or t.startswith(BACKSTOP[0])


def country(t: str) -> str | None:
    """PWRSPVBGDXX -> BGD (the three letters after the six-letter prefix)."""
    t = str(t)
    return t[6:9] if len(t) >= 9 and t.startswith("PWR") else None


def interconnector(t: str) -> str | None:
    m = INTERCONNECTOR.match(str(t))
    return f"{m.group(1)} ↔ {m.group(2)}" if m else None


FAMILY_COLOURS = {lab: c for lab, c in TECH_FAMILIES.values()}
FAMILY_COLOURS.update({lab: c for lab, c in STORAGE.values()})
FAMILY_COLOURS[BACKSTOP[1]] = BACKSTOP[2]
FAMILY_ORDER = ORDERED_FAMILIES + [lab for lab, _ in STORAGE.values()] + [BACKSTOP[1]]


def colours(cats, fixed=None):
    fixed = fixed or {}
    return [fixed.get(c, PAL[i % len(PAL)]) for i, c in enumerate(cats)]


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load_outputs(data: RunData) -> pd.DataFrame:
    if data.outputs_path is None:
        return pd.DataFrame()
    have = set(pd.read_csv(data.outputs_path, nrows=0).columns)
    cols = ["Future_ID"] + [c for c in NEEDED if c in have]
    df = pd.read_csv(data.outputs_path, usecols=cols, low_memory=False, dtype={"Future_ID": str})
    for c in ("TECHNOLOGY", "FUEL", "EMISSION"):
        df[c] = df[c].map(_norm_key) if c in df.columns else ""
    df["YEAR"] = pd.to_numeric(df["YEAR"], errors="coerce")
    df = df[df.YEAR.notna()].copy()
    df["YEAR"] = df.YEAR.astype(int)
    df["Named"] = df.Future_ID.map(data.named)
    df["S"] = df.Future_ID.map(data.sample).astype(str)
    return df


def periods(years: list[int]) -> tuple[list[str], dict[int, str], list[float]]:
    """Five-year cost periods aligned on years ending in 0 and 5 (first and last trimmed)."""
    if not years:
        return [], {}, []
    labels, y2p, n = [], {}, []
    lo = years[0]
    while lo <= years[-1]:
        hi = min(years[-1], (lo // 5) * 5 + (5 if lo % 5 else 4))
        hi = max(hi, lo)
        lab = f"{lo}–{hi}" if hi > lo else str(lo)
        span = [y for y in years if lo <= y <= hi]
        labels.append(lab)
        n.append(float(len(span) or 1))
        for y in span:
            y2p[y] = lab
        lo = hi + 1
    return labels, y2p, n


class Builder:
    def __init__(self, data: RunData, df: pd.DataFrame):
        self.data, self.df = data, df
        self.named = [n for n in dict.fromkeys(data.named(f) for f in data.futures)
                      if n in set(df.Named)]
        self.samples = {n: sorted({data.sample(f) for f in data.futures
                                   if data.named(f) == n and f in set(df.Future_ID)})
                        for n in self.named}
        self.years = sorted(df.YEAR.unique().tolist())
        self.plabels, self.y2p, self.pn = periods(self.years)
        self.missing: list[str] = []

    def has(self, col):
        ok = col in self.df.columns and self.df[col].notna().any()
        if not ok and col not in self.missing:
            self.missing.append(col)
        return ok

    def cube(self, sub: pd.DataFrame, val: str, cats: list[str], xkind="year") -> dict:
        """v[named][sample] = [[value per x] per category]; periods are per-year averages."""
        xs = self.years if xkind == "year" else self.plabels
        xi = {x: i for i, x in enumerate(xs)}
        ci = {c: i for i, c in enumerate(cats)}
        out = {n: {str(s): [[0.0] * len(xs) for _ in cats] for s in self.samples[n]} for n in self.named}
        if sub.empty:
            return out
        sub = sub.copy()
        if xkind == "period":
            sub["X"] = sub.YEAR.map(self.y2p)
        else:
            sub["X"] = sub.YEAR
        g = sub.groupby(["Named", "S", "X", "cat"], observed=True)[val].sum()
        for (n, s, x, c), v in g.items():
            if n in out and s in out[n] and x in xi and c in ci:
                d = self.pn[xi[x]] if xkind == "period" else 1.0
                out[n][s][ci[c]][xi[x]] = _r(v / d)
        return out

    def stack(self, val, mask, catfn, order=None, fixed=None, yt="", xkind="year",
              kind="area", stack=True, scale=1.0, top=None):
        if not self.has(val):
            return None
        sub = self.df[mask & self.df[val].notna()].copy()
        if sub.empty:
            return None
        sub["cat"] = sub.TECHNOLOGY.map(catfn) if callable(catfn) else sub[catfn]
        sub = sub[sub.cat.notna()]
        sub[val] = sub[val] * scale
        present = list(dict.fromkeys(sub.cat))
        cats = [c for c in (order or []) if c in present] + sorted(c for c in present if c not in (order or []))
        if top and len(cats) > top:   # keep the biggest, fold the rest
            size = sub.groupby("cat")[val].apply(lambda s: s.abs().sum()).sort_values(ascending=False)
            keep = set(size.index[:top])
            sub["cat"] = sub.cat.where(sub.cat.isin(keep), "Other")
            cats = [c for c in cats if c in keep] + ["Other"]
        return {"kind": kind, "stack": stack, "cats": cats, "colors": colours(cats, fixed),
                "x": [str(x) for x in (self.years if xkind == "year" else self.plabels)],
                "yt": yt, "v": self.cube(sub, val, cats, xkind)}

    def line(self, series_fn, yt, xkind="year"):
        """series_fn(frame of one future) -> pd.Series indexed by year."""
        xs = self.years
        v = {n: {} for n in self.named}
        for f, g in self.df.groupby("Future_ID"):
            n, s = self.data.named(f), str(self.data.sample(f))
            if n not in v:
                continue
            ser = series_fn(g)
            if ser is None:
                continue
            v[n][s] = [_r(ser.get(y, np.nan)) for y in xs]
        if not any(v.values()):
            return None
        return {"kind": "line", "cats": ["value"], "x": [str(x) for x in xs], "yt": yt,
                "v": {n: {s: [vals] for s, vals in d.items()} for n, d in v.items()}}


def _r(x, n=5):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return round(f, n + 3) if abs(f) < 1 else round(f, n)


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def build_payload(data: RunData) -> dict:
    df = load_outputs(data)
    if df.empty:
        return {"meta": {"empty": True}, "tabs": [], "figs": {}}
    B = Builder(data, df)
    T = df.TECHNOLOGY
    gen = T.map(is_generation)
    stor = T.str.startswith(tuple(STORAGE))
    tabs, figs = [], {}

    def add(key, group, title, note, fig):
        if fig is None:
            return
        figs[key] = fig
        tabs.append({"id": key, "group": group, "title": title, "note": note})

    # ---- Overview -------------------------------------------------------
    if B.has("TotalDiscountedCost"):
        add("cost", "Overview", "System cost",
            "Total discounted system cost per year [M$]. The line is the average of the sampled "
            "futures, the band their range, the dotted line the central future.",
            B.line(lambda g: g.groupby("YEAR").TotalDiscountedCost.sum(), "M$ (discounted)"))
        add("cumcost", "Overview", "Cumulative system cost",
            "Total discounted system cost summed from the first year [M$].",
            B.line(lambda g: g.groupby("YEAR").TotalDiscountedCost.sum().sort_index().cumsum(),
                   "M$ (discounted, cumulative)"))
    if B.has("AnnualEmissions"):
        add("emis", "Overview", "Emissions",
            "Annual emissions of the whole system [Mt CO₂].",
            B.line(lambda g: g.groupby("YEAR").AnnualEmissions.sum(), "Mt CO₂"))
    if B.has("ProductionByTechnologyAnnual"):
        def vre(g):
            gg = g[g.TECHNOLOGY.map(is_generation)]
            tot = gg.groupby("YEAR").ProductionByTechnologyAnnual.sum()
            v = gg[gg.TECHNOLOGY.str.startswith(VRE)].groupby("YEAR").ProductionByTechnologyAnnual.sum()
            return (v.reindex(tot.index).fillna(0) / tot.replace(0, np.nan) * 100)
        add("vre", "Overview", "VRE share",
            "Solar PV and wind as a share of generation [%].", B.line(vre, "% of generation"))

    # ---- Power ------------------------------------------------------------
    add("gen", "Power", "Generation by technology",
        "Electricity generation by technology family [PJ/yr].",
        B.stack("ProductionByTechnologyAnnual", gen, family, FAMILY_ORDER, FAMILY_COLOURS, "PJ/yr"))
    add("cap", "Power", "Capacity by technology",
        "Installed capacity by technology family, storage included [GW].",
        B.stack("TotalCapacityAnnual", gen | stor, family, FAMILY_ORDER, FAMILY_COLOURS, "GW"))
    add("newcap", "Power", "New capacity by technology",
        "Capacity built each year [GW/yr].",
        B.stack("NewCapacity", gen | stor, family, FAMILY_ORDER, FAMILY_COLOURS, "GW/yr", kind="bar"))
    add("stor", "Power", "Storage capacity",
        "Installed storage capacity by duration [GW].",
        B.stack("TotalCapacityAnnual", stor, family, FAMILY_ORDER, FAMILY_COLOURS, "GW"))

    # ---- Countries --------------------------------------------------------
    add("gen_c", "Countries", "Generation by country",
        "Electricity generation by country, from the technology code (PWRSPVBGDXX → BGD) [PJ/yr].",
        B.stack("ProductionByTechnologyAnnual", gen, country, yt="PJ/yr"))
    add("cap_c", "Countries", "Capacity by country",
        "Installed generation capacity by country [GW].",
        B.stack("TotalCapacityAnnual", gen, country, yt="GW"))
    if B.has("AnnualEmissions"):
        B.df = B.df.assign(EC=B.df.EMISSION.str[-3:])
        add("emis_c", "Countries", "Emissions by country",
            "Annual emissions by country, from the emission code (CO2BGD → BGD) [Mt CO₂].",
            B.stack("AnnualEmissions", B.df.EMISSION.ne(""), "EC", yt="Mt CO₂"))

    # ---- Costs --------------------------------------------------------------
    parts = [(c, lab, col) for c, lab, col in COST_PARTS if B.has(c)]
    if parts:
        long = pd.concat([df[df[c].notna()].assign(cat=lab, VAL=df[c])[["Named", "S", "YEAR", "cat", "VAL"]]
                          for c, lab, col in parts], ignore_index=True)
        cats = [lab for _, lab, _ in parts]
        add("costparts", "Costs", "Cost components by period",
            "Capital investment, fixed and variable O&M, undiscounted, as an average per year of "
            "each period [M$/yr].",
            {"kind": "bar", "stack": True, "cats": cats, "colors": [c for *_, c in parts],
             "x": B.plabels, "yt": "M$/yr", "v": B.cube(long, "VAL", cats, "period")})
    add("capinv", "Costs", "Capital investment by technology",
        "Capital investment by technology family, average per year of each period [M$/yr].",
        B.stack("CapitalInvestment", gen | stor, family, FAMILY_ORDER, FAMILY_COLOURS, "M$/yr",
                xkind="period", kind="bar"))
    add("dcost", "Costs", "Discounted cost by technology",
        "Discounted cost by technology family, average per year of each period [M$/yr].",
        B.stack("DiscountedCostByTechnology", gen | stor, family, FAMILY_ORDER, FAMILY_COLOURS,
                "M$/yr", xkind="period", kind="bar"))

    # ---- Interconnectors ----------------------------------------------------
    ic = T.map(lambda t: interconnector(t) is not None)
    add("ic_flow", "Interconnectors", "Interconnector flow",
        "Electricity carried by each interconnector, both directions [PJ/yr].",
        B.stack("ProductionByTechnologyAnnual", ic, interconnector, yt="PJ/yr", kind="line", stack=False))
    add("ic_cap", "Interconnectors", "Interconnector capacity",
        "Installed capacity of each interconnector [GW].",
        B.stack("TotalCapacityAnnual", ic, interconnector, yt="GW", kind="line", stack=False))

    # ---- Robustness ---------------------------------------------------------
    metrics = {}
    last = B.years[-1] if B.years else None
    per_future = {}
    for f, g in df.groupby("Future_ID"):
        m = {}
        if "TotalDiscountedCost" in g and g.TotalDiscountedCost.notna().any():
            m["Total discounted cost [M$]"] = g.TotalDiscountedCost.sum()
        if "AnnualEmissions" in g and g.AnnualEmissions.notna().any():
            m["Cumulative emissions [Mt CO₂]"] = g.AnnualEmissions.sum()
            m[f"Emissions in {last} [Mt CO₂]"] = g.loc[g.YEAR == last, "AnnualEmissions"].sum()
        if "ProductionByTechnologyAnnual" in g:
            gg = g[g.TECHNOLOGY.map(is_generation) & (g.YEAR == last)]
            tot = gg.ProductionByTechnologyAnnual.sum()
            if tot:
                m[f"VRE share in {last} [%]"] = (
                    gg[gg.TECHNOLOGY.str.startswith(VRE)].ProductionByTechnologyAnnual.sum() / tot * 100)
        per_future[f] = m
    names = sorted({k for m in per_future.values() for k in m}, key=lambda k: (
        ["Total", "Cumulative", "Emissions", "VRE"].index(k.split()[0]) if k.split()[0] in
        ("Total", "Cumulative", "Emissions", "VRE") else 9, k))
    for k in names:
        metrics[k] = {n: [[data.sample(f), _r(per_future[f][k])] for f in per_future
                          if data.named(f) == n and k in per_future[f]] for n in B.named}
    if metrics:
        add("spread", "Robustness", "Spread across futures",
            "Each dot is one future; the box spans the sampled futures of a named future and the "
            "diamond marks its central future. A wide box means the result depends on the "
            "uncertainties; boxes that do not overlap mean the named futures differ whatever the "
            "draw.", {"kind": "dist", "metrics": metrics})
        cost_k = next((k for k in names if k.startswith("Total discounted")), None)
        emis_k = next((k for k in names if k.startswith("Cumulative emissions")), None)
        if cost_k and emis_k:
            add("tradeoff", "Robustness", "Cost vs emissions",
                "Each point is one future: total discounted cost against cumulative emissions. "
                "Down and to the left is cheaper and cleaner.",
                {"kind": "scatter", "xk": cost_k, "yk": emis_k, "metrics": metrics})

    unsolved = [f for f in data.futures if data.status_of(f) != SOLVED]
    meta = {
        "run": data.ref.label, "named": B.named, "samples": B.samples,
        "names": {n: _future_name(data, n) for n in B.named},
        "ref": B.named[0] if B.named else "", "years": [B.years[0], B.years[-1]] if B.years else [],
        "solved": len(set(df.Future_ID)), "unsolved": unsolved,
        "missing": [m for m in B.missing if m in NEEDED],
        "generated": pd.Timestamp.now().strftime("%d %b %Y %H:%M"),
    }
    return {"meta": meta, "tabs": tabs, "figs": figs}


def _future_name(data: RunData, named: str) -> str:
    f = data.interface.futures
    if len(f) and "Future_ID" in f.columns and "Name" in f.columns:
        row = f[f.Future_ID.astype(str) == named]
        if len(row) and str(row.Name.iloc[0]) not in ("", "nan"):
            return f"{named} · {row.Name.iloc[0]}"
    return named


def _plotly_script() -> str:
    try:
        from plotly.offline import get_plotlyjs
        return f"<script>{get_plotlyjs()}</script>"
    except Exception:
        return f'<script src="{CDN}"></script>'


def build_outputs_dashboard(data: RunData, path: Path) -> Path | None:
    payload = build_payload(data)
    if payload["meta"].get("empty"):
        return None
    js = json.dumps(payload, allow_nan=False, default=lambda o: None).replace("</", "<\\/")
    page = (TEMPLATE.replace("__TITLE__", html.escape(f"RDM results · {data.ref.label}"))
            .replace("__PLOTLY__", _plotly_script()) + "const D = " + js + ";\n" + SCRIPT)
    path.write_text(page, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__TITLE__</title>
__PLOTLY__
<style>
:root{--ink:#13334f;--mut:#5a6b80;--rule:#e3e8ee;--bg:#f6f8fa;--card:#fff;--accent:#2a6f97;--bad:#a03027}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{background:var(--ink);color:#fff;padding:14px 22px}
header h1{margin:0;font-size:18px;font-weight:600;letter-spacing:.2px}
header .sub{opacity:.78;font-size:12.5px;margin-top:3px}
#ctrl{position:sticky;top:0;z-index:20;background:var(--card);border-bottom:1px solid var(--rule);
      padding:10px 22px;display:flex;flex-wrap:wrap;gap:22px;align-items:center}
.grp{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.glab{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--mut);font-weight:700}
label{font-size:13px;display:inline-flex;align-items:center;gap:5px;cursor:pointer}
select{font:13px inherit;padding:4px 8px;border:1px solid var(--rule);border-radius:5px;background:#fff;color:var(--ink)}
.pill{background:var(--bg);border:1px solid var(--rule);border-radius:999px;padding:3px 11px;font-size:12px;color:var(--mut)}
.pill.warn{background:#fdecea;border-color:#f3c2bc;color:var(--bad)}
#wrap{display:flex;align-items:flex-start}
#nav{width:232px;flex:0 0 232px;padding:14px 10px 40px;border-right:1px solid var(--rule);background:var(--card);
     min-height:calc(100vh - 120px);position:sticky;top:52px;max-height:calc(100vh - 52px);overflow:auto}
#nav .ghead{font-size:10.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--mut);font-weight:700;margin:14px 10px 6px}
#nav .ghead:first-child{margin-top:0}
.navbtn{display:block;width:100%;text-align:left;border:0;background:none;cursor:pointer;font:13px inherit;
        color:var(--ink);padding:6px 10px;border-radius:6px}
.navbtn:hover{background:var(--bg)} .navbtn.active{background:var(--ink);color:#fff}
#main{flex:1;min-width:0;padding:18px 22px 40px}
.card{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:14px 16px}
.card h2{margin:0 0 4px;font-size:16px} .note{color:var(--mut);font-size:13px;margin-bottom:10px;max-width:980px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(380px,1fr))}
.plot{height:440px;min-width:0} .plot.small{height:400px}
.msg{color:var(--mut);padding:30px;text-align:center}
footer{color:var(--mut);font-size:12px;padding:10px 22px 30px}
@media (max-width:900px){#wrap{flex-direction:column}#nav{width:100%;flex:none;position:static;max-height:none;min-height:0}
 .grid{grid-template-columns:1fr}}
</style></head><body>
<header><h1 id="title"></h1><div class="sub" id="subtitle"></div></header>
<div id="ctrl">
  <div class="grp"><span class="glab">Named futures</span><span id="named_boxes"></span></div>
  <div class="grp"><span class="glab">Show</span><select id="view"></select></div>
  <div class="grp"><span class="glab">Values</span><select id="mode">
     <option value="abs">As they are</option><option value="diff">Difference from reference</option></select>
     <select id="refsel" hidden></select></div>
  <div class="grp"><span class="pill" id="note"></span><span class="pill warn" id="unsolved" hidden></span></div>
</div>
<div id="wrap"><div id="nav"></div><div id="main"></div></div>
<footer id="foot"></footer>
<script>
"""

SCRIPT = r"""
const M=D.meta, FIGS=D.figs, TABS=D.tabs, NAMED=M.named;
const STRPAL=["#13334f","#c0392b","#2a6f97","#c9a227","#5a8f69","#8e6f9c","#c97f4a","#4aa0b9"];
const col=n=>STRPAL[NAMED.indexOf(n)%STRPAL.length];
const lab=n=>M.names[n]||n;
document.getElementById("title").textContent="OSTRAM RDM results — "+M.run;
const nSamp=Math.max(0,...Object.values(M.samples).map(a=>a.filter(s=>s>0).length));
document.getElementById("subtitle").textContent=NAMED.length+" named future(s) · "+M.solved+
  " solved future(s) · "+(M.years.length?M.years[0]+"–"+M.years[1]:"")+" · built "+M.generated;
document.getElementById("named_boxes").innerHTML=NAMED.map(n=>
  '<label><input type="checkbox" id="n_'+n+'" checked><span style="color:'+col(n)+'">■</span>'+lab(n)+'</label>').join(" ");
const allS=[...new Set(Object.values(M.samples).flat())].sort((a,b)=>a-b);
document.getElementById("view").innerHTML=
  '<option value="avg">Average of sampled futures</option><option value="0">Central future (no sampling)</option>'+
  allS.filter(s=>s>0).map(s=>'<option value="'+s+'">Sample s'+String(s).padStart(3,"0")+'</option>').join("");
const refSel=document.getElementById("refsel");
refSel.innerHTML=NAMED.map(n=>'<option value="'+n+'">'+lab(n)+'</option>').join("");
if(M.unsolved.length){const u=document.getElementById("unsolved");u.hidden=false;
  u.textContent="Not solved, no results: "+M.unsolved.join(", ");}
document.getElementById("foot").textContent=M.missing.length?
  "Not in RDM_Outputs.csv (Export = N on the Outputs sheet), so their tabs are left out: "+M.missing.join(", "):"";
const NAMEDSEL=()=>NAMED.filter(n=>document.getElementById("n_"+n).checked);
const VIEW=()=>document.getElementById("view").value;
const MODE=()=>document.getElementById("mode").value;
const REF=()=>refSel.value;
const esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]);

// matrix [cat][x] for one named future under the current view; null if absent
function sampled(F,n){return Object.keys(F.v[n]||{}).filter(s=>s!=="0");}
function matrix(F,n,view){
  const b=F.v[n]||{}; view=view||VIEW();
  if(view==="avg"){const ks=sampled(F,n).length?sampled(F,n):Object.keys(b); if(!ks.length) return null;
    return F.cats.map((_,ci)=>F.x.map((_,xi)=>{let s=0,k=0;ks.forEach(f=>{const v=b[f][ci][xi];if(v!=null){s+=v;k++;}});return k?s/k:null;}));}
  return b[view]?b[view]:null;
}
function withDiff(F,n,Mx,view){
  if(MODE()!=="diff"||!Mx) return Mx;
  const R=matrix(F,REF(),view); if(!R) return null;
  return Mx.map((r,ci)=>r.map((v,xi)=>(v==null||R[ci][xi]==null)?null:v-R[ci][xi]));
}
function viewNote(){
  const v=VIEW();
  const base=v==="avg"?"Average of each named future's sampled futures (central future if it has none)":
    v==="0"?"Central futures only":"Sample s"+String(v).padStart(3,"0")+" of every named future";
  document.getElementById("note").textContent=base+(MODE()==="diff"?" · minus "+lab(REF()):"");
}
function layout(title,yt,extra){return Object.assign({title:{text:title,font:{size:13}},
  margin:{l:62,r:12,t:36,b:46},paper_bgcolor:"#fff",plot_bgcolor:"#fff",hovermode:"x unified",
  legend:{orientation:"h",y:-0.16,font:{size:11}},font:{family:"Segoe UI,Roboto,Helvetica,Arial,sans-serif",size:12,color:"#13334f"},
  xaxis:{gridcolor:"#eef1f5"},yaxis:{title:{text:yt},gridcolor:"#eef1f5",zerolinecolor:"#c3ccd6"}},extra||{});}
const CFG={displaylogo:false,responsive:true,modeBarButtonsToRemove:["lasso2d","select2d"]};

function drawStack(host,F){
  const ns=NAMEDSEL(); host.innerHTML='<div class="grid"></div>'; const g=host.firstChild;
  if(!ns.length){host.innerHTML='<div class="msg">Tick a named future above.</div>';return;}
  const diff=MODE()==="diff", yt=F.yt+(diff?" (difference)":"");
  // create every panel first, so each is measured at its final grid width
  const divs=ns.map(()=>{const d=document.createElement("div");d.className="plot"+(ns.length>1?" small":"");g.appendChild(d);return d;});
  ns.forEach((n,i)=>{
    const d=divs[i];
    if(diff&&n===REF()){d.innerHTML='<div class="msg">'+esc(lab(n))+' is the reference: the other panels show their difference from it.</div>';return;}
    const Mx=withDiff(F,n,matrix(F,n),VIEW());
    if(!Mx){d.innerHTML='<div class="msg">'+esc(lab(n))+': no result for this view.</div>';return;}
    const tr=F.cats.map((c,ci)=>{
      const y=Mx[ci]; if(y.every(v=>!v)) return null;
      const t={x:F.x,y:y,name:c,legendgroup:c,marker:{color:F.colors[ci]},
        hovertemplate:c+": %{y:,.4~g}<extra></extra>"};
      if(F.kind==="bar") return Object.assign(t,{type:"bar"});
      if(F.stack&&!diff) return Object.assign(t,{type:"scatter",mode:"lines",stackgroup:"a",line:{width:.6,color:F.colors[ci]}});
      return Object.assign(t,{type:"scatter",mode:"lines+markers",line:{width:2,color:F.colors[ci]},marker:{size:4,color:F.colors[ci]}});
    }).filter(Boolean);
    Plotly.newPlot(d,tr,layout(lab(n),yt,{barmode:(F.stack&&!diff)?"relative":"group"}),CFG);
  });
}

function drawLine(host,F){
  host.innerHTML='<div class="plot"></div>'; const d=host.firstChild;
  const tr=[]; const diff=MODE()==="diff";
  NAMEDSEL().forEach(n=>{
    const b=F.v[n]||{}; const ks=sampled(F,n);
    const sub=(y,ref)=>y.map((v,i)=>(v==null||ref==null||ref[i]==null)?v:(v-ref[i]));
    const refFor=view=>{if(!diff) return null; const r=matrix(F,REF(),view); return r?r[0]:null;};
    if(VIEW()==="avg"){
      if(ks.length){
        const rows=ks.map(k=>b[k][0]);
        const lo=F.x.map((_,i)=>Math.min(...rows.map(r=>r[i]).filter(v=>v!=null)));
        const hi=F.x.map((_,i)=>Math.max(...rows.map(r=>r[i]).filter(v=>v!=null)));
        const rref=refFor("avg");
        tr.push({x:F.x,y:sub(hi,rref),mode:"lines",line:{width:0},hoverinfo:"skip",showlegend:false,legendgroup:n});
        tr.push({x:F.x,y:sub(lo,rref),mode:"lines",line:{width:0},fill:"tonexty",fillcolor:col(n)+"22",
          hoverinfo:"skip",showlegend:false,legendgroup:n});
      }
      const m=matrix(F,n,"avg"); if(m) tr.push({x:F.x,y:sub(m[0],refFor("avg")),mode:"lines",name:lab(n)+(ks.length?" (avg of "+ks.length+")":""),
        legendgroup:n,line:{color:col(n),width:2.6},hovertemplate:lab(n)+": %{y:,.5~g}<extra></extra>"});
      if(b["0"]&&ks.length) tr.push({x:F.x,y:sub(b["0"][0],refFor("0")),mode:"lines",name:lab(n)+" central",
        legendgroup:n,line:{color:col(n),width:1.4,dash:"dot"},hovertemplate:lab(n)+" central: %{y:,.5~g}<extra></extra>"});
    } else {
      const m=matrix(F,n); if(m) tr.push({x:F.x,y:sub(m[0],refFor(VIEW())),mode:"lines+markers",name:lab(n),
        line:{color:col(n),width:2.4},marker:{size:4},hovertemplate:lab(n)+": %{y:,.5~g}<extra></extra>"});
    }
  });
  const absent=NAMEDSEL().filter(n=>!matrix(F,n));
  if(absent.length) host.insertAdjacentHTML("beforeend",'<div class="note">No result for this view (not solved or not run): '+
    absent.map(n=>esc(lab(n))).join(", ")+'</div>');
  if(!tr.length){d.outerHTML='<div class="msg">Nothing to show for this view.</div>';return;}
  Plotly.newPlot(d,tr,layout("",F.yt+(diff?" (difference from "+lab(REF())+")":"")),CFG);
}

function drawDist(host,F){
  host.innerHTML='<div class="grid"></div>'; const g=host.firstChild;
  const ents=Object.entries(F.metrics);
  const divs=ents.map(()=>{const d=document.createElement("div");d.className="plot small";g.appendChild(d);return d;});
  ents.forEach(([k,by],i)=>{
    const d=divs[i]; const tr=[];
    NAMEDSEL().forEach(n=>{
      const pts=(by[n]||[]); const s=pts.filter(p=>p[0]>0), c=pts.filter(p=>p[0]===0);
      if(s.length) tr.push({type:"box",y:s.map(p=>p[1]),name:lab(n),marker:{color:col(n)},boxpoints:"all",
        jitter:.4,pointpos:0,text:s.map(p=>"s"+String(p[0]).padStart(3,"0")),
        hovertemplate:"%{text}: %{y:,.5~g}<extra>"+esc(lab(n))+"</extra>",showlegend:false});
      if(c.length) tr.push({type:"scatter",mode:"markers",x:[lab(n)],y:[c[0][1]],name:lab(n)+" central",
        marker:{symbol:"diamond",size:12,color:"#fff",line:{color:col(n),width:2}},
        hovertemplate:"central: %{y:,.5~g}<extra>"+esc(lab(n))+"</extra>",showlegend:false});
    });
    Plotly.newPlot(d,tr,layout(k,"",{hovermode:"closest",showlegend:false}),CFG);
  });
}

function drawScatter(host,F){
  host.innerHTML='<div class="plot" style="height:520px"></div>'; const d=host.firstChild; const tr=[];
  NAMEDSEL().forEach(n=>{
    const X=new Map((F.metrics[F.xk][n]||[]).map(p=>[p[0],p[1]])), Y=new Map((F.metrics[F.yk][n]||[]).map(p=>[p[0],p[1]]));
    const ks=[...X.keys()].filter(k=>Y.has(k));
    const s=ks.filter(k=>k>0), c=ks.filter(k=>k===0);
    if(s.length) tr.push({x:s.map(k=>X.get(k)),y:s.map(k=>Y.get(k)),mode:"markers",name:lab(n),
      text:s.map(k=>"s"+String(k).padStart(3,"0")),marker:{color:col(n),size:9,opacity:.8},
      hovertemplate:"%{text}<br>cost %{x:,.6~g}<br>emissions %{y:,.5~g}<extra>"+esc(lab(n))+"</extra>"});
    if(c.length) tr.push({x:[X.get(0)],y:[Y.get(0)],mode:"markers",name:lab(n)+" central",
      marker:{symbol:"diamond",size:14,color:"#fff",line:{color:col(n),width:2.5}},
      hovertemplate:"central<br>cost %{x:,.6~g}<br>emissions %{y:,.5~g}<extra>"+esc(lab(n))+"</extra>"});
  });
  Plotly.newPlot(d,tr,layout("",F.yk,{xaxis:{title:{text:F.xk},gridcolor:"#eef1f5"},hovermode:"closest",
    legend:{orientation:"h",y:-0.14}}),CFG);
}

const NAV=document.getElementById("nav"), MAIN=document.getElementById("main"); let CUR=0; const BTN=[];
let lastG=null;
TABS.forEach((t,i)=>{ if(t.group!==lastG){const h=document.createElement("div");h.className="ghead";h.textContent=t.group;NAV.appendChild(h);lastG=t.group;}
  const b=document.createElement("button"); b.className="navbtn"; b.textContent=t.title; b.onclick=()=>show(i); NAV.appendChild(b); BTN.push(b);});
function show(i){CUR=i;BTN.forEach((b,j)=>b.classList.toggle("active",j===i));
  const t=TABS[i],F=FIGS[t.id];
  MAIN.innerHTML='<div class="card"><h2>'+esc(t.title)+'</h2><div class="note">'+esc(t.note)+'</div><div id="host"></div></div>';
  const host=document.getElementById("host");
  const viewable=F.kind==="line"||F.kind==="area"||F.kind==="bar";
  document.getElementById("view").disabled=!viewable; document.getElementById("mode").disabled=!viewable;
  ({line:drawLine,area:drawStack,bar:drawStack,dist:drawDist,scatter:drawScatter}[F.kind])(host,F);
  viewNote();
}
["view","mode"].forEach(id=>document.getElementById(id).onchange=()=>{refSel.hidden=MODE()!=="diff";show(CUR);});
refSel.onchange=()=>show(CUR);
NAMED.forEach(n=>document.getElementById("n_"+n).onchange=()=>show(CUR));
if(TABS.length) show(0); else MAIN.innerHTML='<div class="msg">RDM_Outputs.csv holds none of the variables these tabs use.</div>';
</script></body></html>
"""
