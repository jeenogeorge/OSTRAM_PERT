"""How the run perturbed its inputs: tables for the workbook and the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .excel import Sheet, write_workbook
from .levers import (applies, default_years, drawn, feasibility_relevant, lever_rows,
                     lever_summary, position, ratio_table, realised)
from .run import KEY_COLUMNS, SOLVED, Lever, RunData

NONNEGATIVE_EXEMPT: set[str] = set()
UNIT_INTERVAL = {"AvailabilityFactor", "CapacityFactor"}


@dataclass
class Perturbation:
    """Everything the workbook and the dashboard show about one run."""

    reference: str
    years: list[int]
    levers: pd.DataFrame          # one row per lever
    by_future: pd.DataFrame       # one row per lever x future
    values: pd.DataFrame          # one row per lever x member key x future, years as columns
    ratios: pd.DataFrame          # same, divided by the reference future
    checks: pd.DataFrame          # things that look off
    rows: dict                    # lever_id -> long rows (all years), for the dashboard


def analyse(data: RunData, years: list[int] | None = None, reference: str | None = None) -> Perturbation:
    reference = data.reference_future(reference)
    params = sorted({lv.parameter for lv in data.interface.levers if lv.parameter})
    all_rows = data.input_values(params)
    years = years or default_years(all_rows["YEAR"].unique())

    lever_recs, future_recs, value_frames, ratio_frames, checks = [], [], [], [], []
    rows_by_lever = {}
    positions_by_lever = {}
    for lever in data.interface.levers:
        rec = lever_summary(lever)
        rows = lever_rows(data, lever)
        rows_by_lever[lever.lever_id] = rows
        if rows.empty:
            available = lever.parameter in all_rows.columns and all_rows[lever.parameter].notna().any()
            problem = ("Selector matches nothing in the inputs" if available else
                       "Parameter not available in RDM_Inputs.csv (set Export = Y on the Outputs sheet)")
            checks.append(_check(lever, "", problem, f"{lever.selector_type} = {lever.selector}"))
            rec.update(Members_Matched=0, Members="", Rows_Per_Future=0, Check=problem)
            lever_recs.append(rec)
            continue

        members = sorted(rows["Member"].unique())
        per_future = rows.groupby("Future_ID").size()
        rec.update(Members_Matched=len(members),
                   Members=", ".join(members[:8]) + (" …" if len(members) > 8 else ""),
                   Rows_Per_Future=int(per_future.median()) if len(per_future) else 0)

        real = realised(data, lever, rows, sorted(rows.YEAR.dropna().unique().astype(int)))
        lever_checks = []
        positions = {}
        for f in data.futures:
            ap = applies(data, lever, f)
            d = drawn(data, lever, f)
            pos = position(lever, d)
            positions[f] = pos
            r = real.get(f, np.nan)
            verdict = _verdict(lever, ap, d, r)
            if verdict not in ("ok", "", "not checkable"):
                lever_checks.append(f)
                checks.append(_check(lever, f, verdict,
                                     f"drawn {d:.6g}, data show {r:.6g}" if np.isfinite(r) else ""))
            future_recs.append({
                "Lever": lever.lever_id, "Kind": lever.kind, "Parameter": lever.parameter,
                "Future_ID": f, "Named_Future": data.named(f), "Sample": data.sample(f),
                "Solver_Status": data.status_of(f), "Applies": ap,
                "Drawn": d, "Position_in_Range": pos, "Realised_in_Data": r, "Check": verdict,
            })
        positions_by_lever[lever.lever_id] = positions
        rec["Check"] = (f"look at {', '.join(lever_checks[:5])}" if lever_checks else "ok")
        lever_recs.append(rec)

        # physical range
        bad = rows[(rows.Value < 0) | (rows.Parameter.isin(UNIT_INTERVAL) & (rows.Value > 1 + 1e-9))]
        for f, g in bad.groupby("Future_ID"):
            checks.append(_check(lever, f, "Value outside its physical range",
                                 f"{len(g)} row-years, e.g. {g.Member.iloc[0]} {int(g.YEAR.iloc[0])}: {g.Value.iloc[0]:.4g}"))

        # wide tables at the reporting years
        keys = [c for c in KEY_COLUMNS if rows[c].astype(str).ne("").any()]
        at = rows[rows.YEAR.isin(years)]
        if at.empty:
            continue
        wide = at.pivot_table(index=keys + ["Future_ID"], columns="YEAR", values="Value",
                              aggfunc="first").reset_index()
        wide.columns = [int(c) if isinstance(c, float) else c for c in wide.columns]
        ycols = [y for y in years if y in wide.columns]
        spread = wide.groupby(keys)[ycols].agg(lambda s: s.max() - s.min()).abs().max(axis=1)
        varies = (spread > 1e-12).rename("Varies_Across_Futures").reset_index()
        wide = wide.merge(varies, on=keys, how="left")
        wide = _decorate(data, lever, wide, keys)
        value_frames.append(wide[_front(wide, keys) + ycols])

        rt = ratio_table(data, lever, at, reference)
        if not rt.empty:
            rl = rt.stack().rename("Ratio").reset_index()
            rl = rl.pivot_table(index=keys + ["Future_ID"], columns="YEAR", values="Ratio",
                                aggfunc="first").reset_index()
            rl.columns = [int(c) if isinstance(c, float) else c for c in rl.columns]
            rl["Varies_Across_Futures"] = rl.merge(varies, on=keys, how="left")["Varies_Across_Futures"].values
            rl = _decorate(data, lever, rl, keys)
            ratio_frames.append(rl[_front(rl, keys) + [y for y in years if y in rl.columns]])

    # linked uncertainties should sit at the same position in their ranges
    for lever in data.interface.levers:
        if lever.kind != "Uncertainty" or not lever.linked_to:
            continue
        a, b = positions_by_lever.get(lever.lever_id, {}), positions_by_lever.get(lever.linked_to, {})
        apart = [f for f in a if np.isfinite(a.get(f, np.nan)) and np.isfinite(b.get(f, np.nan))
                 and abs(a[f] - b[f]) > 1e-6]
        if apart:
            checks.append(_check(lever, ", ".join(apart[:6]), "Linked levers moved apart",
                                 f"{lever.lever_id} is linked to {lever.linked_to}"))

    levers_df = pd.DataFrame(lever_recs)
    by_future = pd.DataFrame(future_recs)
    values = _concat(value_frames)
    ratios = _concat(ratio_frames)
    checks_df = pd.DataFrame(checks, columns=["Lever", "Kind", "Parameter", "Future_ID", "Check", "Detail"])
    return Perturbation(reference, years, levers_df, by_future, values, ratios, checks_df, rows_by_lever)


def _verdict(lever: Lever, ap: bool, d: float, r: float) -> str:
    if not ap:
        return ""
    if lever.method == "Copy_From_Tech" or not np.isfinite(d):
        return "not checkable"
    if not np.isfinite(r):
        return "not checkable"
    tol = 1e-3 * max(1.0, abs(d))
    if lever.method in ("Multiply", "Multiply_Ramp") and abs(d - 1) > tol and abs(r - 1) <= 1e-6:
        return "Perturbation did not land"
    if abs(r - d) > tol:
        return "Data differ from the drawn value"
    return "ok"


def _check(lever: Lever, future: str, check: str, detail: str) -> dict:
    return {"Lever": lever.lever_id, "Kind": lever.kind, "Parameter": lever.parameter,
            "Future_ID": future, "Check": check, "Detail": detail}


def _decorate(data: RunData, lever: Lever, wide: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    wide = wide.copy()
    wide.insert(0, "Lever", lever.lever_id)
    wide.insert(1, "Kind", lever.kind)
    wide.insert(2, "Parameter", lever.parameter)
    wide["Named_Future"] = wide.Future_ID.map(data.named)
    wide["Sample"] = wide.Future_ID.map(data.sample)
    wide["Solver_Status"] = wide.Future_ID.map(data.status_of)
    wide["Applies"] = wide.Future_ID.map(lambda f: applies(data, lever, f))
    order = {f: i for i, f in enumerate(data.futures)}
    return wide.sort_values(keys + ["Future_ID"], key=lambda s: s.map(order) if s.name == "Future_ID" else s)


def _front(df: pd.DataFrame, keys: list[str]) -> list[str]:
    return (["Lever", "Kind", "Parameter"] + keys +
            ["Future_ID", "Named_Future", "Sample", "Solver_Status", "Applies", "Varies_Across_Futures"])


def _concat(frames):
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    year_cols = sorted(c for c in df.columns if isinstance(c, int))
    # set columns not used by a lever stay blank
    for c in KEY_COLUMNS:
        if c in df.columns:
            df[c] = df[c].fillna("")
    keys = [c for c in KEY_COLUMNS if c in df.columns]
    head = ["Lever", "Kind", "Parameter"] + keys
    other = [c for c in df.columns if not isinstance(c, int) and c not in head]
    return df[head + other + year_cols]


# ---------------------------------------------------------------------------
def _years_text(years: list[int]) -> str:
    if years and years == list(range(years[0], years[-1] + 1)):
        return f"every year {years[0]}–{years[-1]}"
    return "at " + ", ".join(map(str, years))


def write_excel(data: RunData, p: Perturbation, path: Path) -> Path:
    unsolved = set(data.unsolved)

    def status_fill(rec):
        return "red" if rec.get("Solver_Status", SOLVED) != SOLVED else None

    def check_fill(rec):
        c = rec.get("Check", "")
        return None if c in ("ok", "", "not checkable") else "amber"

    design = data.design.merge(data.status[["Future_ID", "Solver_Status", "Solver_Message"]],
                               on="Future_ID", how="left")
    readme = pd.DataFrame({"Sheet": [
        "Levers", "Draws", "Lever_by_Future", "Perturbed_Values", f"Ratio_to_{p.reference}",
        "Checks", "Run_Status"], "What it shows": [
        "One row per lever: every Uncertainty_Table row (sampled) and every Future_Changes row "
        "(fixed), the members its selector reached in the inputs, and whether the data look right.",
        "RDM_Experiment_Design.csv with each future's solver status: the value every "
        "uncertainty was drawn at in every future.",
        "One row per lever and future: the drawn value, where it sits in the Min–Max range "
        "(0 = Min, 1 = Max), and what the input data actually show at the lever's last year.",
        f"The perturbed input values themselves, {_years_text(p.years)}, one row per "
        "lever, member and future. Unsolved futures are red.",
        f"The same rows divided by the reference future {p.reference}.",
        "Things that look off: selectors that matched nothing, parameters not exported, "
        "draws that did not reach the data, values outside their physical range.",
        "RDM_Run_Status.csv plus the solver status read from each future's solution file.",
    ]})
    sheets = [
        Sheet("README", readme, intro=[
            f"Perturbed inputs — run {data.ref.label}",
            f"Run folder: {data.ref.path}",
            f"Reference future: {p.reference}   ·   Futures: {len(data.futures)}   ·   "
            f"Unsolved: {len(unsolved)}" + (f" ({', '.join(sorted(unsolved))})" if unsolved else ""),
        ] + [f"Note: {w}" for w in data.warnings], max_width=110),
        Sheet("Levers", p.levers, row_fill=check_fill),
        Sheet("Draws", design, row_fill=status_fill),
        Sheet("Lever_by_Future", p.by_future,
              row_fill=lambda r: "amber" if check_fill(r) else status_fill(r)),
        Sheet("Perturbed_Values", p.values, row_fill=status_fill, freeze_cols=3),
        Sheet(f"Ratio_to_{p.reference}", p.ratios, row_fill=status_fill, freeze_cols=3),
        Sheet("Checks", p.checks if len(p.checks) else
              pd.DataFrame({"Check": ["Nothing looks off."]})),
        Sheet("Run_Status", data.status, row_fill=status_fill),
    ]
    return write_workbook(path, sheets)
