"""Why did a future not solve?  Is one of the perturbed parameters the cause?

For every unsolved future (or the futures you name), three independent lines
of evidence are put side by side with the futures that did solve:

1. **Levers.**  Each uncertainty's draw in the unsolved future is placed
   against the draws of the solved futures.  A draw beyond every solved draw,
   in the direction that tightens the model (more demand, a lower cap, a
   higher minimum ...), makes that lever a suspect.  A fixed change of a named
   future is a suspect only if no solved future carries it.  Cost levers
   (CapitalCost, FixedCost, VariableCost) change the optimum, never
   feasibility, and are listed but never blamed.
2. **Input rows, all sets.**  Every input row that differs between futures
   (Technology, Commodity, Emission, UDC, Storage, Region), year by year, is
   checked against the range the solved futures span.  This catches knock-on
   effects that no lever names directly.
3. **Constraint conflicts.**  Pairs of bounds that cannot both hold, checked
   in the unsolved future and in every solved one: minimum above maximum new
   capacity, minimum above maximum total capacity, residual capacity above the
   total-capacity cap, activity lower limit above the upper limit, an
   availability factor outside 0-1.  A conflict counts only if no solved
   future has it too.

The solver's own status line and log are added as evidence.  A future that
*stopped* (time limit) or never ran is not an infeasibility; the report says
so.  With few solved futures the comparison is weak, and the report says
that as well.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .allsets import SetListing, _Source
from .excel import Sheet, write_workbook
from .levers import applies, drawn, feasibility_relevant, tightening
from .perturbed import Perturbation
from .run import (INFEASIBLE, KEY_COLUMNS, NO_SOLUTION, NOT_COMPILED, SOLVED, STATUS_HELP,
                  STOPPED, UNKNOWN, RunData, _text, future_folders)

# lhs must not exceed rhs, on the same REGION / TECHNOLOGY / YEAR.
PAIRS = [
    ("TotalAnnualMinCapacityInvestment", "TotalAnnualMaxCapacityInvestment",
     "minimum new capacity above the maximum new capacity"),
    ("TotalAnnualMinCapacity", "TotalAnnualMaxCapacity",
     "minimum total capacity above the maximum total capacity"),
    ("ResidualCapacity", "TotalAnnualMaxCapacity",
     "residual capacity above the total-capacity cap"),
    ("TotalTechnologyAnnualActivityLowerLimit", "TotalTechnologyAnnualActivityUpperLimit",
     "activity lower limit above the activity upper limit"),
    ("TotalTechnologyModelPeriodActivityLowerLimit", "TotalTechnologyModelPeriodActivityUpperLimit",
     "model-period activity lower limit above the upper limit"),
]
UNIT_RANGE = {"AvailabilityFactor", "CapacityFactor"}
NONNEGATIVE = {"SpecifiedAnnualDemand", "AccumulatedAnnualDemand", "ResidualCapacity",
               "OperationalLife", "InputActivityRatio", "OutputActivityRatio"}
CHECK_PARAMS = sorted({p for a, b, _ in PAIRS for p in (a, b)} | UNIT_RANGE | NONNEGATIVE)

SCORE_TEXT = {3: "Suspect", 2: "Possible", 1: "Weak", 0: "Unlikely"}


@dataclass
class Diagnosis:
    investigated: list[str]
    compared_with: list[str]
    summary: pd.DataFrame
    levers: pd.DataFrame
    rows: pd.DataFrame
    conflicts: pd.DataFrame
    evidence: pd.DataFrame
    notes: list[str]


# ---------------------------------------------------------------------------
# 1. Levers
# ---------------------------------------------------------------------------
def rank_levers(data: RunData, p: Perturbation, future: str, ok: list[str]) -> pd.DataFrame:
    recs = []
    lbf = p.by_future.set_index(["Lever", "Future_ID"]) if len(p.by_future) else None
    for lv in data.interface.levers:
        feas = feasibility_relevant(lv.parameter)
        tight = tightening(lv.parameter)
        ap = applies(data, lv, future)
        d = drawn(data, lv, future)
        ok_ap = [f for f in ok if applies(data, lv, f)]
        ok_d = np.array([drawn(data, lv, f) for f in ok_ap], float)
        ok_d = ok_d[np.isfinite(ok_d)]
        lo = float(ok_d.min()) if ok_d.size else np.nan
        hi = float(ok_d.max()) if ok_d.size else np.nan
        med = float(np.median(ok_d)) if ok_d.size else np.nan
        sd = float(ok_d.std()) if ok_d.size > 1 else np.nan
        z = (d - ok_d.mean()) / sd if (np.isfinite(d) and np.isfinite(sd) and sd > 0) else np.nan
        score, why = 0, ""
        if not ap:
            why = "Not applied in this future."
        elif not feas:
            why = "Cost parameter: changes the optimum, never feasibility."
        elif lv.kind == "Change":
            if ok_ap:
                why = f"Also applied in solved future(s) {', '.join(ok_ap[:4])}: cannot cause it on its own."
            else:
                score, why = 2, "No solved future carries this change; compare with a solved future of another named future."
        elif not np.isfinite(d):
            why = "No draw recorded for this future."
        elif not ok_d.size:
            score, why = 1, "No solved future applies this lever, so there is nothing to compare with."
        else:
            above, below = d > hi + 1e-12, d < lo - 1e-12
            toward = {"up": d > med, "down": d < med, "both": d != med}.get(tight, False)
            beyond = {"up": above, "down": below, "both": above or below}.get(tight, False)
            if beyond:
                score = 3
                why = (f"Drawn {d:.4g}, {'above' if above else 'below'} every solved draw "
                       f"({lo:.4g}–{hi:.4g}), in the direction that tightens the model.")
            elif above or below:
                score = 1
                why = (f"Drawn {d:.4g}, outside the solved draws ({lo:.4g}–{hi:.4g}) but in the "
                       "direction that relaxes the model.")
            elif toward and ok_d.size >= 2 and _tail(d, ok_d, tight):
                score = 1
                why = f"Drawn {d:.4g}, inside the solved draws ({lo:.4g}–{hi:.4g}) but at their tight end."
            else:
                why = f"Drawn {d:.4g}, inside the solved draws ({lo:.4g}–{hi:.4g})."
        landed = ""
        if lbf is not None and (lv.lever_id, future) in lbf.index:
            chk = lbf.loc[(lv.lever_id, future), "Check"]
            if chk not in ("ok", "", "not checkable"):
                landed = chk
        recs.append({
            "Future_ID": future, "Lever": lv.lever_id, "Kind": lv.kind, "Name": lv.name,
            "Parameter": lv.parameter, "Selector": f"{lv.selector_type} {lv.selector}",
            "Can_Affect_Feasibility": "yes" if feas else "no (cost)",
            "Tightens_When": {"up": "higher", "down": "lower", "both": "either way", None: "-"}[tight],
            "Applied_Here": ap, "Drawn_Here": d,
            "Solved_Min": lo, "Solved_Median": med, "Solved_Max": hi, "Z_vs_Solved": z,
            "N_Solved_Applying": len(ok_ap), "Score": score, "Verdict": SCORE_TEXT[score],
            "Reason": why, "Data_Check": landed,
        })
    df = pd.DataFrame(recs)
    if len(df):
        df["_abs_z"] = df.Z_vs_Solved.abs().fillna(0)
        df = df.sort_values(["Score", "_abs_z"], ascending=False).drop(columns="_abs_z")
    return df


def _tail(d: float, ok: np.ndarray, tight: str) -> bool:
    q = np.quantile(ok, [0.25, 0.75])
    return {"up": d >= q[1], "down": d <= q[0], "both": d <= q[0] or d >= q[1]}.get(tight, False)


# ---------------------------------------------------------------------------
# 2. Input rows outside what the solved futures span
# ---------------------------------------------------------------------------
def rows_outside(data: RunData, listing: SetListing, future: str, ok: list[str]) -> pd.DataFrame:
    long = listing.long
    if long is None or long.empty or future not in set(long.Future_ID):
        return pd.DataFrame()
    keys = ["Parameter"] + KEY_COLUMNS + ["YEAR"]
    lg = long.copy()
    lg["YEAR"] = lg["YEAR"].fillna(-1)
    mine = lg[lg.Future_ID == future].set_index(keys)["Value"]
    mine = mine[~mine.index.duplicated()]
    solved = lg[lg.Future_ID.isin(ok)]
    if solved.empty:
        return pd.DataFrame()
    env = solved.groupby(keys)["Value"].agg(["min", "max", "count"])
    j = pd.concat([mine.rename("v"), env], axis=1, join="outer")
    j = j[j.v.notna() | j["count"].notna()]
    tol = 1e-9 + 1e-6 * j[["min", "max"]].abs().max(axis=1).fillna(0)
    above = j.v > j["max"] + tol
    below = j.v < j["min"] - tol
    only_here = j.v.notna() & j["count"].isna()
    missing_here = j.v.isna() & j["count"].notna()
    out = j[above | below | only_here | missing_here].copy()
    if out.empty:
        return pd.DataFrame()
    out["Direction"] = np.select(
        [above[out.index], below[out.index], only_here[out.index]],
        ["above", "below", "only in this future"], "missing in this future")
    with np.errstate(divide="ignore", invalid="ignore"):
        ex_up = (out.v - out["max"]) / out["max"].abs().replace(0, np.nan)
        ex_dn = (out["min"] - out.v) / out["min"].abs().replace(0, np.nan)
    out["Rel_Excess"] = np.where(out.Direction == "above", ex_up,
                                 np.where(out.Direction == "below", ex_dn, np.nan))
    out = out.reset_index()

    recs = []
    for k, g in out.groupby(["Parameter"] + KEY_COLUMNS, sort=False):
        par = k[0]
        dirs = sorted(set(g.Direction))
        tight = tightening(par)
        tightens = any((d == "above" and tight in ("up", "both")) or
                       (d == "below" and tight in ("down", "both")) for d in dirs)
        feas = feasibility_relevant(par)
        member = next((v for c, v in zip(KEY_COLUMNS, k[1:]) if c in ("TECHNOLOGY", "FUEL") and v), "")
        yrs = sorted(int(y) for y in g.YEAR if y != -1)
        worst = g.loc[g.Rel_Excess.abs().idxmax()] if g.Rel_Excess.notna().any() else g.iloc[0]
        recs.append({
            "Future_ID": future, "Parameter": par,
            **{c: v for c, v in zip(KEY_COLUMNS, k[1:])},
            "Direction": ", ".join(dirs), "Years_Outside": len(g),
            "First_Year": yrs[0] if yrs else "", "Last_Year": yrs[-1] if yrs else "",
            "Worst_Year": int(worst.YEAR) if worst.YEAR != -1 else "",
            "Value_Here": worst.v, "Solved_Min": worst["min"], "Solved_Max": worst["max"],
            "Max_Rel_Excess": float(g.Rel_Excess.abs().max()) if g.Rel_Excess.notna().any() else np.nan,
            "Can_Affect_Feasibility": "yes" if feas else "no (cost)",
            "Tightens_Model": "yes" if (feas and tightens) else "no",
            "Lever": ", ".join(_levers_reaching(data, par, member)),
        })
    df = pd.DataFrame(recs)
    df["_r"] = (df.Tightens_Model == "yes").astype(int) * 2 + (df.Can_Affect_Feasibility == "yes").astype(int)
    df = df.sort_values(["_r", "Max_Rel_Excess", "Years_Outside"], ascending=False).drop(columns="_r")
    return df.drop(columns=[c for c in KEY_COLUMNS if c in df.columns and df[c].eq("").all()])


def _levers_reaching(data: RunData, parameter: str, member: str) -> list[str]:
    """Ids of the levers whose parameter and selector reach this TECHNOLOGY / FUEL."""
    return [lv.lever_id for lv in data.interface.levers
            if lv.parameter == parameter and member and lv.members([member])]


# ---------------------------------------------------------------------------
# 3. Constraint conflicts
# ---------------------------------------------------------------------------
def conflicts_in(long: pd.DataFrame) -> pd.DataFrame:
    """Pairs of bounds that cannot both hold, and values outside their physical range."""
    cols = ["Check", "Parameter", "Against", "REGION", "TECHNOLOGY", "FUEL", "YEAR", "Value", "Bound"]
    if long.empty:
        return pd.DataFrame(columns=cols)
    recs = []
    k = ["REGION", "TECHNOLOGY", "YEAR"]
    for lhs, rhs, text in PAIRS:
        a = long[long.Parameter == lhs]
        b = long[long.Parameter == rhs]
        if a.empty or b.empty:
            continue
        m = a[k + ["Value"]].merge(b[k + ["Value"]], on=k, suffixes=("", "_b"))
        m = m[(m.Value_b >= 0) & (m.Value > m.Value_b * (1 + 1e-6) + 1e-9)]   # -1 = no cap
        for r in m.itertuples():
            recs.append({"Check": text, "Parameter": lhs, "Against": rhs, "REGION": r.REGION,
                         "TECHNOLOGY": r.TECHNOLOGY, "FUEL": "", "YEAR": r.YEAR,
                         "Value": r.Value, "Bound": r.Value_b})
    u = long[long.Parameter.isin(UNIT_RANGE) & ((long.Value < -1e-9) | (long.Value > 1 + 1e-9))]
    for r in u.itertuples():
        recs.append({"Check": "factor outside 0–1", "Parameter": r.Parameter, "Against": "",
                     "REGION": r.REGION, "TECHNOLOGY": r.TECHNOLOGY, "FUEL": r.FUEL,
                     "YEAR": r.YEAR, "Value": r.Value, "Bound": "0–1"})
    n = long[long.Parameter.isin(NONNEGATIVE) & (long.Value < -1e-9)]
    for r in n.itertuples():
        recs.append({"Check": "negative value", "Parameter": r.Parameter, "Against": "",
                     "REGION": r.REGION, "TECHNOLOGY": r.TECHNOLOGY, "FUEL": r.FUEL,
                     "YEAR": r.YEAR, "Value": r.Value, "Bound": "≥ 0"})
    return pd.DataFrame(recs, columns=cols)


def conflict_table(data: RunData, future: str, ok: list[str], cache: dict) -> tuple[pd.DataFrame, list[str]]:
    src = _Source(data, "Input", False, CHECK_PARAMS)

    def get(f):
        if f not in cache:
            cache[f] = src.long(f) if src.where(f) else pd.DataFrame()
        return cache[f]

    mine = get(future)
    notes = []
    if mine.empty:
        return pd.DataFrame(), [f"{future}: no input values reachable; constraint checks skipped."]
    have = set(mine.Parameter)
    skipped = [f"{a} vs {b}" for a, b, _ in PAIRS if not ({a, b} <= have)]
    if skipped:
        notes.append("Bound pairs not checked, because a parameter is not among the inputs read "
                     "(not in the model, or not exported to RDM_Inputs.csv): " + "; ".join(skipped))
    here = conflicts_in(mine)
    if here.empty:
        return here, notes
    key = ["Check", "Parameter", "REGION", "TECHNOLOGY", "FUEL", "YEAR"]
    seen = {}
    for f in ok:
        c = conflicts_in(get(f))
        for t in c[key].itertuples(index=False):
            seen.setdefault(tuple(t), []).append(f)
    here = here.copy()
    here.insert(0, "Future_ID", future)
    here["Also_In_Solved"] = [", ".join(seen.get(tuple(t), [])[:5]) for t in here[key].itertuples(index=False)]
    here["New_In_This_Future"] = here.Also_In_Solved.eq("")
    return here.sort_values(["New_In_This_Future", "Check", "TECHNOLOGY", "YEAR"],
                            ascending=[False, True, True, True]), notes


# ---------------------------------------------------------------------------
# 4. What the solver said
# ---------------------------------------------------------------------------
KEYWORDS = ("infeas", "unbounded", "time limit", "stopped", "error", "abort", "conflict", "primal inf")


def solver_evidence(data: RunData, future: str) -> list[dict]:
    rec = data.status.loc[data.status.Future_ID == future].iloc[0].to_dict()
    out = [{"Future_ID": future, "Source": "status", "File": rec.get("Sol_File", ""),
            "Line": f"{rec.get('Solver_Status')}: {STATUS_HELP.get(rec.get('Solver_Status'), '')}"}]
    if _text(rec.get("Solver_Message")):
        out.append({"Future_ID": future, "Source": "solution file", "File": rec.get("Sol_File", ""),
                    "Line": rec["Solver_Message"]})
    for folder in [f for f in future_folders(data.ref, rec) if f.is_dir()][:1]:
        for log in sorted(folder.glob("*.log")):
            try:
                lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            hits = [ln.strip() for ln in lines if any(k in ln.lower() for k in KEYWORDS)]
            for ln in hits[-6:]:
                out.append({"Future_ID": future, "Source": log.name, "File": str(log), "Line": ln[:300]})
        for fo in folder.glob("*.feasopt.sol"):
            out.append({"Future_ID": future, "Source": "feasopt", "File": str(fo),
                        "Line": "CPLEX feasopt solution present: open it to see which constraints were relaxed."})
    return out


# ---------------------------------------------------------------------------
def diagnose(data: RunData, p: Perturbation, listing: SetListing,
             futures: list[str] | None = None) -> Diagnosis:
    notes = []
    if futures:
        unknown = [f for f in futures if f not in data.futures]
        if unknown:
            notes.append(f"Not futures of this run, skipped: {', '.join(unknown)}")
        futures = [f for f in futures if f in data.futures]
    else:
        futures = data.unsolved
    ok = [f for f in data.solved if f not in futures]
    if not futures:
        notes.append("Every future solved: nothing to diagnose. Name futures to compare them anyway.")
    if futures and len(ok) < 3:
        notes.append(f"Only {len(ok)} solved future(s) to compare with: the comparison is weak, "
                     "treat 'suspects' as leads to check, not conclusions.")

    lever_frames, row_frames, conflict_frames, ev, summ = [], [], [], [], []
    cache: dict = {}
    for f in futures:
        lv = rank_levers(data, p, f, ok)
        rw = rows_outside(data, listing, f, ok)
        cf, cnotes = conflict_table(data, f, ok, cache)
        notes += [n for n in cnotes if n not in notes]
        lever_frames.append(lv)
        row_frames.append(rw)
        conflict_frames.append(cf)
        ev += solver_evidence(data, f)
        summ.append(_verdict(data, f, lv, rw, cf, len(ok)))

    def cat(frames):
        frames = [x for x in frames if x is not None and len(x)]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    return Diagnosis(futures, ok, pd.DataFrame(summ), cat(lever_frames), cat(row_frames),
                     cat(conflict_frames), pd.DataFrame(ev), notes)


def _verdict(data, f, lv, rw, cf, n_ok) -> dict:
    status = data.status_of(f)
    causes = []
    if len(cf):
        new = cf[cf.New_In_This_Future]
        for (check, par, tech, fuel), g in list(new.groupby(["Check", "Parameter", "TECHNOLOGY", "FUEL"],
                                                            sort=False))[:3]:
            yrs = sorted(int(y) for y in g.YEAR.dropna())
            span = (f"{yrs[0]}–{yrs[-1]}" if len(yrs) > 1 else str(yrs[0])) if yrs else ""
            w = g.iloc[0]
            bound = w.Bound if isinstance(w.Bound, str) else f"{w.Bound:.4g}"
            causes.append(f"{par} {tech or fuel} {span}: {check} "
                          f"(e.g. {w.Value:.4g} vs {bound}, {len(g)} year(s))")
    sus = lv[lv.Score >= 3] if len(lv) else lv
    for r in sus.head(3).itertuples():
        causes.append(f"{r.Lever} {r.Name or r.Parameter}: {r.Reason}")
    tight_rows = rw[rw.Tightens_Model == "yes"] if len(rw) else rw
    in_conflict = set()
    if len(cf):
        new = cf[cf.New_In_This_Future]
        in_conflict = set(zip(new.Parameter, new.TECHNOLOGY))
    shown = 0
    for r in tight_rows.itertuples():
        member = next((getattr(r, c) for c in ("TECHNOLOGY", "FUEL", "EMISSION", "UDC", "STORAGE")
                       if c in rw.columns and getattr(r, c)), "")
        if (r.Parameter, getattr(r, "TECHNOLOGY", "")) in in_conflict or shown == 3:
            continue          # already named as a conflict above
        shown += 1
        how = (f"{r.Direction} the solved range in {r.Years_Outside} year(s), up to {r.Max_Rel_Excess:.1%}"
               if np.isfinite(r.Max_Rel_Excess) else r.Direction)
        who = f"lever {r.Lever}" if r.Lever else "no lever names this row: a knock-on or unlisted change"
        causes.append(f"{r.Parameter} {member}: {how} ({who})")
    new_conf = int(cf.New_In_This_Future.sum()) if len(cf) else 0
    if new_conf or len(sus):
        verdict = "Likely input cause"
    elif len(tight_rows):
        verdict = "Possible input cause"
    else:
        verdict = "No perturbed input sets this future apart"
        causes.append("The perturbed inputs of this future sit inside what the solved futures span."
                      + ("" if status == SOLVED else " Look at the solver log (time limit, numerical "
                         "trouble) rather than the draws."))
    if status in (STOPPED,):
        verdict = "Solver stopped (not proven infeasible); " + verdict[0].lower() + verdict[1:]
    elif status in (NO_SOLUTION, NOT_COMPILED, UNKNOWN):
        verdict = f"Status {status}: " + verdict[0].lower() + verdict[1:]
    return {
        "Future_ID": f, "Named_Future": data.named(f), "Sample": data.sample(f),
        "Solver_Status": status, "Verdict": verdict,
        "Constraint_Conflicts_New": new_conf,
        "Suspect_Levers": ", ".join(sus.Lever) if len(sus) else "",
        "Possible_Levers": ", ".join(lv[lv.Score == 2].Lever) if len(lv) else "",
        "Tightening_Rows_Outside_Solved": len(tight_rows),
        "Compared_With_N_Solved": n_ok,
        "Leads": "\n".join(f"• {c}" for c in causes[:7]),
    }


# ---------------------------------------------------------------------------
def unsolved_table(data: RunData) -> pd.DataFrame:
    st = data.status.copy()
    st = st[st.Solver_Status != SOLVED]
    st["What_It_Means"] = st.Solver_Status.map(STATUS_HELP)
    cols = ["Future_ID", "Named_Future", "Sample", "Status", "Solver_Status", "Solver_Message",
            "What_It_Means", "Sol_File", "Input_CSV"]
    return st[[c for c in cols if c in st.columns]]


def write_excel(data: RunData, dx: Diagnosis, path: Path) -> Path:
    def by_verdict(r):
        v = str(r.get("Verdict", ""))
        return "red" if "Likely" in v or r.get("Score") == 3 else ("amber" if "Possible" in v or r.get("Score") == 2 else None)

    readme = pd.DataFrame({"Sheet": [
        "Summary", "Unsolved_Futures", "Lever_Ranking", "Inputs_Outside_Solved",
        "Constraint_Conflicts", "Solver_Evidence"], "What it shows": [
        "One row per investigated future: the verdict and the leads to follow, strongest first.",
        "Every future that did not solve, with the solver's own status line.",
        "Every lever, for every investigated future: its draw against the draws of the solved "
        "futures, whether it can affect feasibility, and a score (3 Suspect … 0 Unlikely).",
        "Input rows of every set (Technology, Commodity, Emission, UDC, Storage, Region) whose "
        "values lie outside what the solved futures span, year by year, with the lever that reaches "
        "them. 'Tightens_Model = yes' rows are the ones that can make the LP infeasible.",
        "Bounds that cannot both hold in this future (min above max, residual above cap, factor "
        "outside 0–1). New_In_This_Future = TRUE when no solved future has the same conflict.",
        "The solver status line and the matching lines of the solver log.",
    ]})
    intro = [f"Infeasibility diagnosis — run {data.ref.label}",
             f"Run folder: {data.ref.path}",
             f"Investigated: {', '.join(dx.investigated) or 'none'}   ·   "
             f"compared with the solved futures: {', '.join(dx.compared_with) or 'none'}",
             "A lever or input is a lead, not a proof: confirm by re-running the future with that "
             "lever at a solved value."] + [f"Note: {n}" for n in dx.notes]

    def nonempty(df, msg):
        return df if len(df) else pd.DataFrame({"Note": [msg]})

    sheets = [
        Sheet("README", readme, intro=intro, max_width=110),
        Sheet("Summary", nonempty(dx.summary, "Nothing to diagnose: every future solved."),
              row_fill=by_verdict, max_width=120),
        Sheet("Unsolved_Futures", nonempty(unsolved_table(data), "Every future solved."), max_width=80),
        Sheet("Lever_Ranking", nonempty(dx.levers, "No levers."), row_fill=by_verdict, freeze_cols=2, max_width=90),
        Sheet("Inputs_Outside_Solved", nonempty(dx.rows, "No input row lies outside what the solved futures span."),
              row_fill=lambda r: "red" if r.get("Tightens_Model") == "yes" else None, freeze_cols=2),
        Sheet("Constraint_Conflicts", nonempty(dx.conflicts, "No conflicting bounds found."),
              row_fill=lambda r: "red" if r.get("New_In_This_Future") is True else None, freeze_cols=2),
        Sheet("Solver_Evidence", nonempty(dx.evidence, "No solver files reachable."), max_width=120),
    ]
    return write_workbook(path, sheets)


def print_report(data: RunData, dx: Diagnosis) -> None:
    if not dx.investigated:
        print("  Every future solved; nothing to diagnose.")
    for r in dx.summary.itertuples():
        print(f"\n  {r.Future_ID}  [{r.Solver_Status}]  ->  {r.Verdict}")
        for line in str(r.Leads).splitlines():
            print(f"     {line}")
    for n in dx.notes:
        print(f"  note: {n}")
