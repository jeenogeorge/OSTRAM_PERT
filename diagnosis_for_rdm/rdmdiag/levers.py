"""Tie each lever of the interface to the input rows it changes, future by future."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .run import KEY_COLUMNS, Lever, RunData, _text

# What a change in each parameter does to feasibility.
#   "up"    a larger value tightens the problem (can make it infeasible)
#   "down"  a smaller value tightens it
#   "both"  either direction can (e.g. residual capacity above a cap)
#   None    cost-type: changes the optimum only, never feasibility
TIGHTENS = {
    "SpecifiedAnnualDemand": "up", "AccumulatedAnnualDemand": "up",
    "AvailabilityFactor": "down", "CapacityFactor": "down",
    "ResidualCapacity": "both", "ResidualStorageCapacity": "both",
    "TotalAnnualMaxCapacity": "down", "TotalAnnualMinCapacity": "up",
    "TotalAnnualMaxCapacityInvestment": "down", "TotalAnnualMinCapacityInvestment": "up",
    "TotalTechnologyAnnualActivityUpperLimit": "down",
    "TotalTechnologyAnnualActivityLowerLimit": "up",
    "OperationalLife": "down", "ReserveMargin": "up",
    "EmissionActivityRatio": "up", "AnnualEmissionLimit": "down",
    "InputActivityRatio": "up", "OutputActivityRatio": "down",
    "CapitalCost": None, "FixedCost": None, "VariableCost": None, "CapitalCostStorage": None,
}


def tightening(parameter: str) -> str | None:
    return TIGHTENS.get(parameter, "both")


def feasibility_relevant(parameter: str) -> bool:
    return tightening(parameter) is not None


def default_years(all_years) -> list[int]:
    """First year, every year ending in 0 or 5, and the last year."""
    ys = sorted({int(y) for y in all_years if pd.notna(y)})
    if not ys:
        return []
    keep = {ys[0], ys[-1]} | {y for y in ys if y % 5 == 0}
    return sorted(keep)


def applies(data: RunData, lever: Lever, future: str) -> bool:
    """Whether the lever is applied in ``future``."""
    named = data.named(future)
    if lever.kind == "Change":
        return named == lever.applies_to[0]
    if data.sample(future) == 0:          # central futures carry no sampled uncertainty
        return False
    return lever.applies_to is None or named in lever.applies_to


def drawn(data: RunData, lever: Lever, future: str) -> float:
    """The value the lever took in ``future`` (sampled draw, or the change's Value)."""
    if not applies(data, lever, future):
        return np.nan
    if lever.kind == "Change":
        return lever.minimum if lever.minimum is not None else np.nan
    if lever.lever_id not in data.design.columns:
        return np.nan
    v = data.design.loc[data.design.Future_ID == future, lever.lever_id]
    return float(pd.to_numeric(v, errors="coerce").iloc[0]) if len(v) else np.nan


def position(lever: Lever, value: float) -> float:
    """Where a draw sits in the lever's range: 0 at Min, 1 at Max."""
    if lever.kind != "Uncertainty" or lever.minimum is None or lever.maximum is None:
        return np.nan
    span = lever.maximum - lever.minimum
    return (value - lever.minimum) / span if span else np.nan


def check_year(lever: Lever, years: list[int]) -> int | None:
    """The year at which the lever's full effect should be visible."""
    if not years:
        return None
    if lever.year_to:
        return lever.year_to
    return years[-1]


def lever_rows(data: RunData, lever: Lever) -> pd.DataFrame:
    """Every input row the lever's selector reaches, in every future (long, all years)."""

    df = data.input_values([lever.parameter])
    if df.empty or lever.parameter not in df.columns:
        return pd.DataFrame()
    df = df[df[lever.parameter].notna()]
    members = lever.members(df[lever.key_column].unique())
    df = df[df[lever.key_column].isin(members)].copy()
    if df.empty:
        return df
    df = df.rename(columns={lever.parameter: "Value"})
    df["Lever"] = lever.lever_id
    df["Parameter"] = lever.parameter
    df["Member"] = df[lever.key_column]
    return df


def ratio_table(data: RunData, lever: Lever, rows: pd.DataFrame, reference: str) -> pd.DataFrame:
    """Rows x years x futures with each value divided by the reference future's value."""

    if rows.empty:
        return rows
    keys = [c for c in KEY_COLUMNS if rows[c].astype(str).ne("").any()]
    piv = rows.pivot_table(index=keys + ["YEAR"], columns="Future_ID", values="Value", aggfunc="first")
    if reference not in piv.columns:
        return pd.DataFrame()
    base = piv[reference].replace(0, np.nan)
    return piv.div(base, axis=0)


def realised(data: RunData, lever: Lever, rows: pd.DataFrame, years: list[int]) -> dict[str, float]:
    """What the data show the lever did in each future it applies to.

    Multiply / Multiply_Ramp: median ratio, at the check year, to the future's
    own central future (or the reference future when the named future has no
    central run and no change of its own on this parameter).
    Set: the median value at the check year.
    """

    out: dict[str, float] = {}
    if rows.empty:
        return out
    year = check_year(lever, years)
    at = rows[rows.YEAR == year] if year is not None else rows
    if at.empty:
        return out
    keys = [c for c in KEY_COLUMNS if at[c].astype(str).ne("").any()]
    piv = at.pivot_table(index=keys, columns="Future_ID", values="Value", aggfunc="first")
    reference = data.reference_future()
    changed_named = {lv.applies_to[0] for lv in data.interface.levers
                     if lv.kind == "Change" and lv.parameter == lever.parameter}
    for f in piv.columns:
        if not applies(data, lever, f):
            continue
        if lever.method == "Set":
            out[f] = float(piv[f].median())
            continue
        comp = data.central_of(f) if lever.kind == "Uncertainty" else None
        if comp is None:
            comp = reference
            if lever.kind == "Uncertainty" and data.named(f) in changed_named and data.named(f) != reference:
                continue        # the reference differs by the future's own changes too
        if comp == f or comp not in piv.columns:
            continue
        r = piv[f] / piv[comp].replace(0, np.nan)
        r = r[np.isfinite(r)]
        if len(r):
            out[f] = float(r.median())
    return out


def lever_summary(lever: Lever) -> dict:
    return {
        "Lever": lever.lever_id, "Kind": lever.kind, "Name": lever.name,
        "Applies_To": ", ".join(lever.applies_to) if lever.applies_to else "ALL",
        "Sheet": lever.sheet, "Parameter": lever.parameter,
        "Selector_Type": lever.selector_type, "Selector": lever.selector,
        "Method": lever.method,
        "Min": lever.minimum if lever.kind == "Uncertainty" else None,
        "Max": lever.maximum if lever.kind == "Uncertainty" else None,
        "Value": _text(lever.value) if lever.kind == "Change" else "",
        "Year_From": lever.year_from, "Year_To": lever.year_to,
        "Linked_To": lever.linked_to or "",
        "Can_Affect_Feasibility": "yes" if feasibility_relevant(lever.parameter) else "no (cost)",
        "Tightens_When": {"up": "higher", "down": "lower", "both": "higher or lower", None: "-"}[
            tightening(lever.parameter)],
    }
