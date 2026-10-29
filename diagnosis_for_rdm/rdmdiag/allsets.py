"""Every input (or result) value of a run, year by year, listed by set.

``RDM_Inputs.csv`` carries only the variables ticked ``Export = Y`` on the
interface's Outputs sheet, and most of those are keyed by TECHNOLOGY.  This
module reads, for each future, the most complete source this computer can
reach:

1. the future's own B2 CSV (``Executables/<future>_0/<future>_0_Input.csv``),
   which holds every parameter of the model, including the FUEL-keyed
   InputActivityRatio / OutputActivityRatio and the EMISSION-keyed ones;
2. otherwise the future's rows of ``RDM_Inputs.csv`` / ``RDM_Outputs.csv``.

It then lists the values by set family: one sheet each for Technology,
Commodity (FUEL), Emission, UDC, Storage and Region-only parameters.  A row
appears in every family whose set column it fills, so InputActivityRatio is
listed under Technology *and* under Commodity.  Every model year is a column.

Two passes keep memory small on large models: pass 1 compares every future
with the reference future and streams all values to one CSV; pass 2 collects,
for the workbook, only the rows that differ between futures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .excel import Sheet, write_workbook
from .run import KEY_COLUMNS, META_COLUMNS, SET_COLUMNS, SOLVED, RunData, _norm_key

# (family, set column). Region holds the parameters keyed by none of these.
FAMILIES = [
    ("Technology", "TECHNOLOGY"),
    ("Commodity", "FUEL"),
    ("Emission", "EMISSION"),
    ("UDC", "UDC"),
    ("Storage", "STORAGE"),
    ("Region", None),
]
FAMILY_COLUMNS = [c for _, c in FAMILIES if c]
TIMESLICE_COLUMNS = ["TIMESLICE", "SEASON", "DAYTYPE", "DAILYTIMEBRACKET"]
NO_YEAR = "NoYear"          # column for parameters without a YEAR (e.g. OperationalLife)
EXCEL_ROW_LIMIT = 1_000_000
REL_TOL, ABS_TOL = 1e-6, 1e-9


@dataclass
class SetListing:
    kind: str                                   # "Input" or "Output"
    reference: str
    years: list                                 # int years, plus NO_YEAR if used
    varying: pd.DataFrame                       # wide: keys + Parameter + Future_ID + years
    summary: pd.DataFrame                       # one row per parameter
    sources: pd.DataFrame                       # where each future's values came from
    all_csv: Path | None
    notes: list[str] = field(default_factory=list)
    long: pd.DataFrame | None = None            # varying rows, long (for diagnosis / dashboard)

    def family(self, name: str) -> pd.DataFrame:
        col = dict(FAMILIES)[name]
        v = self.varying
        if v.empty:
            return v
        if col is None:
            mask = np.ones(len(v), bool)
            for c in FAMILY_COLUMNS:
                if c in v.columns:
                    mask &= v[c].astype(str).eq("")
            return v[mask]
        if col not in v.columns:
            return v.iloc[0:0]
        return v[v[col].astype(str).ne("")]


# ---------------------------------------------------------------------------
# Reading one future
# ---------------------------------------------------------------------------
class _Source:
    """Hands out one future's values in long form: keys + Parameter + YEAR + Value."""

    def __init__(self, data: RunData, kind: str, include_timeslice: bool,
                 parameters: list[str] | None = None):
        self.data, self.kind, self.include_timeslice = data, kind, include_timeslice
        self.parameters = set(parameters) if parameters else None
        self.rdm_path = data.inputs_path if kind == "Input" else data.outputs_path
        self._rdm: pd.DataFrame | None = None

    def where(self, future: str) -> str:
        own = self.data.own_csv(future, self.kind)
        if own is not None:
            return str(own)
        if self.rdm_path is not None and future in self._rdm_futures():
            return f"{self.rdm_path.name} (exported variables only)"
        return ""

    def _rdm_futures(self) -> set:
        return set(self._rdm_frame()["Future_ID"].astype(str).unique()) if self.rdm_path else set()

    def _rdm_frame(self) -> pd.DataFrame:
        if self._rdm is None:
            self._rdm = pd.read_csv(self.rdm_path, low_memory=False, dtype={"Future_ID": str})
        return self._rdm

    def long(self, future: str) -> pd.DataFrame:
        own = self.data.own_csv(future, self.kind)
        if own is not None:
            frame = pd.read_csv(own, low_memory=False)
        elif self.rdm_path is not None:
            rdm = self._rdm_frame()
            frame = rdm[rdm["Future_ID"].astype(str) == future]
        else:
            frame = pd.DataFrame()
        return to_long(frame, self.include_timeslice, self.parameters)


def to_long(frame: pd.DataFrame, include_timeslice: bool = False,
            parameters: set | None = None) -> pd.DataFrame:
    """Wide B2 / RDM table -> one value per row, keys normalised to strings."""

    cols = ["Parameter"] + KEY_COLUMNS + ["YEAR", "Value"]
    if frame.empty:
        return pd.DataFrame(columns=cols)
    params = [c for c in frame.columns
              if c not in SET_COLUMNS and c not in META_COLUMNS and not str(c).startswith("Unnamed")]
    if parameters is not None:
        params = [p for p in params if p in parameters]
    params = [p for p in params if pd.api.types.is_numeric_dtype(frame[p]) or
              pd.to_numeric(frame[p], errors="coerce").notna().any()]
    if not params:
        return pd.DataFrame(columns=cols)
    sets = [c for c in SET_COLUMNS if c in frame.columns]
    long = frame[sets + params].melt(id_vars=sets, value_vars=params,
                                     var_name="Parameter", value_name="Value")
    long["Value"] = pd.to_numeric(long["Value"], errors="coerce")
    long = long[long["Value"].notna()]
    for c in KEY_COLUMNS:
        long[c] = long[c].map(_norm_key) if c in long.columns else ""
    if not include_timeslice:
        mask = np.ones(len(long), bool)
        for c in TIMESLICE_COLUMNS:
            mask &= long[c].eq("")
        long = long[mask]
    long["YEAR"] = pd.to_numeric(long["YEAR"], errors="coerce") if "YEAR" in long.columns else np.nan
    return long[cols].reset_index(drop=True)


def _rowkey(long: pd.DataFrame) -> pd.Series:
    parts = [long["Parameter"].astype(str)] + [long[c].astype(str) for c in KEY_COLUMNS]
    key = parts[0]
    for p in parts[1:]:
        key = key + "|" + p
    return key


def _indexed(long: pd.DataFrame) -> pd.Series:
    yr = long["YEAR"].fillna(-1).astype(int)
    s = pd.Series(long["Value"].values, index=pd.MultiIndex.from_arrays([_rowkey(long), yr],
                                                                         names=["rowkey", "yr"]))
    return s[~s.index.duplicated(keep="first")]


def _differs(a: pd.Series, b: pd.Series) -> pd.Series:
    """Row-years where two futures disagree, including a value present in only one."""
    j = pd.concat([a.rename("a"), b.rename("b")], axis=1)
    both = j.a.notna() & j.b.notna()
    gap = (j.a - j.b).abs() > (ABS_TOL + REL_TOL * j.b.abs())
    return (both & gap) | (j.a.notna() ^ j.b.notna())


def _wide(long: pd.DataFrame) -> pd.DataFrame:
    if long.empty:
        return pd.DataFrame()
    df = long.copy()
    df["YEAR"] = df["YEAR"].map(lambda y: NO_YEAR if pd.isna(y) else int(y))
    idx = ["Parameter"] + KEY_COLUMNS + ["Future_ID"]
    w = df.pivot_table(index=idx, columns="YEAR", values="Value", aggfunc="first").reset_index()
    w.columns.name = None
    return w


# ---------------------------------------------------------------------------
def list_by_set(data: RunData, kind: str = "Input", reference: str | None = None,
                include_timeslice: bool = False, write_all_csv: bool = True,
                parameters: list[str] | None = None) -> SetListing:
    reference = data.reference_future(reference)
    src = _Source(data, kind, include_timeslice, parameters)
    futures = data.futures if kind == "Input" else [f for f in data.futures if data.status_of(f) == SOLVED]
    notes = []
    sources = pd.DataFrame([{"Future_ID": f, "Solver_Status": data.status_of(f),
                             "Read_From": src.where(f) or "not available"} for f in data.futures])
    readable = [f for f in futures if src.where(f)]
    if not readable:
        notes.append(f"No {kind.lower()} values could be read for any future.")
        return SetListing(kind, reference, [], pd.DataFrame(), pd.DataFrame(), sources, None, notes)
    if reference not in readable:
        reference = readable[0]
        notes.append(f"Reference future has no {kind.lower()} values here; compared with {reference} instead.")
    partial = [f for f in readable if "exported variables only" in src.where(f)]
    full = [f for f in readable if f not in partial]
    if partial and full:
        notes.append(f"{len(full)} future(s) were read from their own B2 CSV (every parameter) and "
                     f"{len(partial)} from {src.rdm_path.name} (exported variables only). Parameters "
                     "missing from the export look like differences; the Summary sheet marks them.")
    elif partial:
        notes.append(f"Values come from {src.rdm_path.name}, which holds only the variables with "
                     "Export = Y on the interface's Outputs sheet. For every parameter (FUEL-keyed "
                     "InputActivityRatio / OutputActivityRatio, emission limits ...), run this on the "
                     "computer that solved the run, or set Export = Y and re-collect.")

    # ---- pass 1: compare with the reference, stream every value to CSV --------
    ref_long = src.long(reference)
    ref_s = _indexed(ref_long)
    all_csv = data.out_dir / f"{kind}s_by_Set_all_years.csv" if write_all_csv else None
    if all_csv is not None and all_csv.exists():
        all_csv.unlink()
    varying_keys: set = set()
    per_param = {}     # parameter -> dict(stats)
    years: set = set()
    params_by_future: dict[str, set] = {}
    written_cols = None
    for f in readable:
        long = ref_long if f == reference else src.long(f)
        params_by_future[f] = set(long["Parameter"].unique())
        years |= {NO_YEAR if pd.isna(y) else int(y) for y in long["YEAR"].unique()}
        if all_csv is not None and not long.empty:
            w = _wide(long.assign(Future_ID=f))
            w.insert(w.columns.get_loc("Future_ID") + 1, "Solver_Status", data.status_of(f))
            if written_cols is None:
                written_cols = list(w.columns)
            else:
                for c in w.columns:
                    if c not in written_cols:
                        written_cols.append(c)     # rare: a year only some futures have
            w.reindex(columns=written_cols).to_csv(all_csv, mode="a", index=False,
                                                    header=not all_csv.exists())
        if f == reference:
            for p, g in long.groupby("Parameter"):
                d = per_param.setdefault(p, {"rows": 0, "differs_in": set(), "rowyears_differ": 0})
                d["rows"] = int(g.groupby(KEY_COLUMNS).ngroups)
            continue
        diff = _differs(_indexed(long), ref_s)
        hit = diff[diff]
        if len(hit):
            keys = hit.index.get_level_values("rowkey")
            varying_keys |= set(keys)
            for p, n in pd.Series(keys.str.split("|").str[0]).value_counts().items():
                d = per_param.setdefault(p, {"rows": 0, "differs_in": set(), "rowyears_differ": 0})
                d["differs_in"].add(f)
                d["rowyears_differ"] += int(n)
        for p in long["Parameter"].unique():
            per_param.setdefault(p, {"rows": 0, "differs_in": set(), "rowyears_differ": 0})

    year_cols = sorted(y for y in years if y != NO_YEAR) + ([NO_YEAR] if NO_YEAR in years else [])

    # ---- pass 2: the rows that differ, in every future --------------------
    frames = []
    if varying_keys:
        for f in readable:
            long = ref_long if f == reference else src.long(f)
            long = long[_rowkey(long).isin(varying_keys)]
            if len(long):
                frames.append(long.assign(Future_ID=f))
    long_all = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["Parameter"] + KEY_COLUMNS + ["YEAR", "Value", "Future_ID"])
    varying = _decorate(data, _wide(long_all), reference, year_cols)

    # ---- summary ----------------------------------------------------------
    all_params = sorted(set().union(*params_by_future.values())) if params_by_future else []
    srows = []
    for p in all_params:
        d = per_param.get(p, {"rows": 0, "differs_in": set(), "rowyears_differ": 0})
        sub = long_all[long_all.Parameter == p] if len(long_all) else long_all
        fams = _families_of(sub if len(sub) else ref_long[ref_long.Parameter == p])
        missing_in = [f for f in readable if p not in params_by_future[f]]
        srows.append({
            "Parameter": p, "Set_Families": ", ".join(fams),
            "Rows_in_" + reference: d["rows"],
            "Rows_That_Differ": int(sub.groupby(KEY_COLUMNS).ngroups) if len(sub) else 0,
            "Row_Years_That_Differ": d["rowyears_differ"],
            "Futures_Where_It_Differs": ", ".join(sorted(d["differs_in"], key=data.futures.index)),
            "Missing_In": ", ".join(missing_in),
        })
    summary = pd.DataFrame(srows)
    if len(summary):
        summary = summary.sort_values(["Rows_That_Differ", "Parameter"], ascending=[False, True])

    if not ref_long["UDC"].ne("").any():
        notes.append("This model has no UDC (user-defined constraint) set, so there is no UDC sheet.")
    return SetListing(kind, reference, year_cols, varying, summary, sources, all_csv, notes, long_all)


def _families_of(long: pd.DataFrame) -> list[str]:
    out = [name for name, col in FAMILIES if col and col in long.columns and long[col].ne("").any()]
    return out or ["Region"]


def _decorate(data: RunData, wide: pd.DataFrame, reference: str, year_cols: list) -> pd.DataFrame:
    if wide.empty:
        return wide
    ycols = [y for y in year_cols if y in wide.columns]
    keys = ["Parameter"] + KEY_COLUMNS
    w = wide.copy()
    w["Named_Future"] = w.Future_ID.map(data.named)
    w["Sample"] = w.Future_ID.map(data.sample)
    w["Solver_Status"] = w.Future_ID.map(data.status_of)
    # largest relative change from the reference future, over the years
    ref = w[w.Future_ID == reference].set_index(keys)[ycols]
    mine = w.set_index(keys)[ycols]
    base = ref.reindex(mine.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = (mine.values - base.values) / np.where(np.abs(base.values) > 0, np.abs(base.values), np.nan)
    rel = pd.DataFrame(rel, index=mine.index)
    absdiff = pd.DataFrame(np.abs(mine.values - base.values), index=mine.index)
    w["Max_Rel_Change_vs_" + reference] = np.nanmax(np.abs(rel.values), axis=1, initial=0.0) \
        if rel.size else 0.0
    w["Max_Abs_Change_vs_" + reference] = np.nanmax(absdiff.values, axis=1, initial=0.0) \
        if absdiff.size else 0.0
    order = {f: i for i, f in enumerate(data.futures)}
    w["_o"] = w.Future_ID.map(order)
    w = w.sort_values(keys + ["_o"]).drop(columns="_o")
    front = keys + ["Future_ID", "Named_Future", "Sample", "Solver_Status",
                    "Max_Rel_Change_vs_" + reference, "Max_Abs_Change_vs_" + reference]
    return w[front + ycols].reset_index(drop=True)


# ---------------------------------------------------------------------------
def write_excel(data: RunData, listing: SetListing, path: Path) -> Path:
    ref = listing.reference

    def fill(rec):
        if rec.get("Solver_Status", SOLVED) != SOLVED:
            return "red"
        if rec.get("Future_ID") == ref:
            return "grey"
        return None

    def clip(df: pd.DataFrame, name: str) -> pd.DataFrame:
        if len(df) > EXCEL_ROW_LIMIT:
            listing.notes.append(f"{name}: {len(df):,} rows, cut to {EXCEL_ROW_LIMIT:,} for Excel; "
                                 f"the full list is in {listing.all_csv.name if listing.all_csv else 'the CSV'}.")
            return df.iloc[:EXCEL_ROW_LIMIT]
        return df

    def drop_empty_sets(df: pd.DataFrame) -> pd.DataFrame:
        return df.drop(columns=[c for c in KEY_COLUMNS if c in df.columns and df[c].eq("").all()])

    fam_sheets, fam_rows = [], []
    for name, col in FAMILIES:
        df = listing.family(name)
        fam_rows.append({"Sheet": name, "Set column": col or "(none: REGION / YEAR only)",
                         "Rows (row x future)": len(df),
                         "Parameters": ", ".join(sorted(df.Parameter.unique())) if len(df) else ""})
        if name == "UDC" and df.empty:
            continue
        table = drop_empty_sets(clip(df, name)) if len(df) else pd.DataFrame(
            {"Note": [f"No {listing.kind.lower()} value keyed by {col or 'region only'} differs between futures."]})
        fam_sheets.append(Sheet(name, table, row_fill=fill, freeze_cols=2))

    kind = listing.kind
    readme = pd.DataFrame(fam_rows)
    intro = [
        f"{kind}s by set, every year — run {data.ref.label}",
        f"Run folder: {data.ref.path}",
        f"Reference future: {ref} (grey rows). Unsolved futures are red.",
        "Each set sheet lists every row that differs between futures, with every year as a column. "
        "A row appears on every sheet whose set column it fills (InputActivityRatio is under "
        "Technology and Commodity).",
        f"All values, including rows that are the same in every future: "
        f"{listing.all_csv.name if listing.all_csv else '(not written)'}",
    ] + [f"Note: {n}" for n in listing.notes]
    sheets = [Sheet("README", readme, intro=intro, max_width=90),
              Sheet("Summary", listing.summary if len(listing.summary) else
                    pd.DataFrame({"Note": ["Nothing to summarise."]})),
              *fam_sheets,
              Sheet("Sources", listing.sources,
                    row_fill=lambda r: "red" if r.get("Solver_Status") != SOLVED else None, max_width=110)]
    return write_workbook(path, sheets)
