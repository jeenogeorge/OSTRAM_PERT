"""Find RDM run folders and load what they hold.

A run folder is written by ``python -m ostram rdm generate`` / ``rdm run``
and holds:

* ``interface.xlsx``             a copy of the workbook the run came from
* ``rdm_manifest.json``          the futures and the exported variables
* ``RDM_Experiment_Design.csv``  one row per future with its sampled draws
* ``RDM_Run_Status.csv``         solved / compiled / missing per future
* ``RDM_Inputs.csv``             the exported input parameters, all futures
* ``RDM_Outputs.csv``            the exported results, solved futures only

The first three always exist; the last three exist once ``rdm run`` (or
``rdm collect``) has finished.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re

import numpy as np
import pandas as pd

ID_COLUMNS = ["Future_ID", "Named_Future", "Sample"]
# OSTRAM's set columns, plus UDC: the OSTRAM model has no user-defined
# constraints today, but a UDC column is picked up if a model ever carries one.
SET_COLUMNS = [
    "REGION", "YEAR", "TECHNOLOGY", "FUEL", "EMISSION", "UDC", "MODE_OF_OPERATION",
    "TIMESLICE", "STORAGE", "SEASON", "DAYTYPE", "DAILYTIMEBRACKET",
]
# Columns that are bookkeeping, not sets or parameters.
META_COLUMNS = {"Future", "Scenario", "Strategy", "Future.ID", "Future_Name", *ID_COLUMNS}
# Set columns that identify a row other than its year.
KEY_COLUMNS = [c for c in SET_COLUMNS if c != "YEAR"]

TECH_SELECTORS = ("Tech", "Techs", "Tech_Prefix")
FUEL_SELECTORS = ("Fuel", "Fuels", "Fuel_Prefix")

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Finding runs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunRef:
    """Where one run folder is, and the setup it belongs to."""

    name: str
    setup: str          # "" for the default workspace (workspace/rdm/<run>)
    path: Path

    @property
    def label(self) -> str:
        return f"{self.setup} / {self.name}" if self.setup else self.name


def default_workspace() -> Path:
    """``OSTRAM_WORKSPACE`` if set, else ``<repo>/workspace``, as OSTRAM resolves it."""

    env = os.environ.get("OSTRAM_WORKSPACE")
    return Path(env) if env else REPO_ROOT / "workspace"


def find_runs(workspace: Path | str | None = None) -> list[RunRef]:
    """Return every RDM run folder under ``workspace``, newest first."""

    workspace = Path(workspace) if workspace else default_workspace()
    runs: list[RunRef] = []
    candidates = list(workspace.glob("rdm/*/rdm_manifest.json"))
    candidates += list(workspace.glob("setups/*/rdm/*/rdm_manifest.json"))
    for manifest in candidates:
        run_dir = manifest.parent
        setup = run_dir.parents[1].name if run_dir.parents[1].parent.name == "setups" else ""
        runs.append(RunRef(run_dir.name, setup, run_dir))
    runs.sort(key=lambda r: r.path.stat().st_mtime, reverse=True)
    return runs


def resolve_run(run: str | None = None, setup: str | None = None,
                run_dir: str | Path | None = None,
                workspace: str | Path | None = None) -> RunRef:
    """Pick one run by folder path, or by name (and setup if the name is used twice)."""

    if run_dir:
        path = Path(run_dir)
        if not (path / "rdm_manifest.json").is_file():
            raise SystemExit(f"Not an RDM run folder (no rdm_manifest.json): {path}")
        setup_name = path.parents[1].name if path.parents[1].parent.name == "setups" else ""
        return RunRef(path.name, setup_name, path)

    runs = find_runs(workspace)
    if not runs:
        raise SystemExit(
            f"No RDM runs found under {Path(workspace) if workspace else default_workspace()}.\n"
            "Run 'python -m ostram rdm run ...' first, or pass --workspace / --run-dir."
        )
    if run is None:
        return choose_run(runs)

    wanted = _canon(run)
    hits = [r for r in runs if _canon(r.name) == wanted and (setup is None or r.setup == setup)]
    if not hits:
        names = ", ".join(r.label for r in runs)
        raise SystemExit(f"No run called {run!r}{f' in setup {setup!r}' if setup else ''}. Found: {names}")
    if len(hits) > 1:
        names = ", ".join(r.label for r in hits)
        raise SystemExit(f"Run {run!r} exists in more than one setup ({names}); add --setup.")
    return hits[0]


def _canon(name: str) -> str:
    """'test 01', 'test_01', 'Test01' all match the folder test01 / test_01."""
    return re.sub(r"[\s_\-]", "", str(name)).lower()


def choose_run(runs: list[RunRef]) -> RunRef:
    """Ask which run to use (works in a terminal and in Spyder's console)."""

    print("\nRDM runs found (newest first):")
    for i, r in enumerate(runs, 1):
        print(f"  {i:>2}. {r.label}    [{r.path}]")
    while True:
        answer = input(f"Which run? [1-{len(runs)}, Enter = 1]: ").strip()
        if answer == "":
            return runs[0]
        if answer.isdigit() and 1 <= int(answer) <= len(runs):
            return runs[int(answer) - 1]
        matches = [r for r in runs if _canon(r.name) == _canon(answer)]
        if len(matches) == 1:
            return matches[0]
        print("  Not recognised; type the number or the run name.")


# ---------------------------------------------------------------------------
# Interface workbook
# ---------------------------------------------------------------------------
def _text(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    return str(v).strip()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


@dataclass
class Lever:
    """One thing the run perturbs: an uncertainty (sampled) or a future's change (fixed)."""

    lever_id: str           # X_ID for uncertainties; "<Future_ID>.C<n>" for changes
    kind: str               # "Uncertainty" or "Change"
    name: str
    applies_to: tuple[str, ...] | None   # named futures; None = all
    sheet: str
    parameter: str
    selector_type: str
    selector: str
    method: str             # Multiply / Multiply_Ramp / Set / Copy_From_Tech
    minimum: float | None   # uncertainty Min; for a change, its Value when numeric
    maximum: float | None
    value: object = None    # a change's Value (may be a tech name for Copy_From_Tech)
    year_from: int | None = None
    year_to: int | None = None
    linked_to: str | None = None
    note: str = ""
    sheet_row: int = 0

    @property
    def key_column(self) -> str:
        return "FUEL" if self.selector_type in FUEL_SELECTORS else "TECHNOLOGY"

    def members(self, values) -> list[str]:
        """The TECHNOLOGY / FUEL values the selector picks out of ``values``."""

        values = sorted({str(v) for v in values if _text(v)})
        sel = self.selector
        if self.selector_type in ("Tech", "Fuel"):
            return [v for v in values if v == sel]
        if self.selector_type in ("Techs", "Fuels"):
            wanted = {p.strip() for p in sel.split(",") if p.strip()}
            return [v for v in values if v in wanted]
        return [v for v in values if v.startswith(sel)]

    def describe(self) -> str:
        if self.kind == "Uncertainty":
            rng = f"{self.minimum:g}–{self.maximum:g}" if self.minimum is not None else ""
        else:
            rng = _text(self.value)
        yrs = ""
        if self.year_from or self.year_to:
            yrs = f", {self.year_from or ''}–{self.year_to or ''}"
        return f"{self.method} {self.parameter} of {self.selector_type} {self.selector} ({rng}{yrs})"


@dataclass
class Interface:
    settings: dict
    futures: pd.DataFrame
    levers: list[Lever]


def read_interface(path: Path) -> Interface:
    """Read the run's copy of the workbook (no validation: it was validated at generate)."""

    xl = pd.ExcelFile(path)
    settings = {}
    if "Setup" in xl.sheet_names:
        raw = pd.read_excel(xl, "Setup", header=None)
        for _, r in raw.iloc[1:].iterrows():
            if _text(r.iloc[0]):
                settings[_text(r.iloc[0])] = r.iloc[1] if len(r) > 1 else None

    futures = pd.read_excel(xl, "Futures") if "Futures" in xl.sheet_names else pd.DataFrame()
    levers: list[Lever] = []

    if "Uncertainty_Table" in xl.sheet_names:
        ut = pd.read_excel(xl, "Uncertainty_Table")
        for i, r in ut.iterrows():
            x_id = _text(r.get("X_ID"))
            if not x_id:
                continue
            applies = _text(r.get("Applies_To")) or "ALL"
            levers.append(Lever(
                lever_id=x_id, kind="Uncertainty", name=_text(r.get("Name")),
                applies_to=None if applies.upper() == "ALL" else tuple(
                    p.strip() for p in applies.split(",") if p.strip()),
                sheet=_text(r.get("Sheet")), parameter=_text(r.get("Parameter")),
                selector_type=_text(r.get("Selector_Type")), selector=_text(r.get("Selector")),
                method=_text(r.get("Method")), minimum=_num(r.get("Min")), maximum=_num(r.get("Max")),
                year_from=_int(r.get("Year_From")), year_to=_int(r.get("Year_To")),
                linked_to=_text(r.get("Linked_To")) or None, note=_text(r.get("Note")),
                sheet_row=int(i) + 2,
            ))

    if "Future_Changes" in xl.sheet_names:
        fc = pd.read_excel(xl, "Future_Changes")
        counter: dict[str, int] = {}
        for i, r in fc.iterrows():
            fid = _text(r.get("Future_ID"))
            if not fid:
                continue
            counter[fid] = counter.get(fid, 0) + 1
            value = r.get("Value")
            levers.append(Lever(
                lever_id=f"{fid}.C{counter[fid]}", kind="Change", name=_text(r.get("Note")),
                applies_to=(fid,), sheet=_text(r.get("Sheet")), parameter=_text(r.get("Parameter")),
                selector_type=_text(r.get("Selector_Type")), selector=_text(r.get("Selector")),
                method=_text(r.get("Operation")), minimum=_num(value), maximum=_num(value),
                value=value, year_from=_int(r.get("Year_From")), year_to=_int(r.get("Year_To")),
                note=_text(r.get("Note")), sheet_row=int(i) + 2,
            ))
    return Interface(settings, futures, levers)


def _int(v):
    f = _num(v)
    return None if f is None else int(f)


# ---------------------------------------------------------------------------
# Solver status of each future
# ---------------------------------------------------------------------------
SOLVED, INFEASIBLE, STOPPED, UNBOUNDED, VIOLATION, NO_SOLUTION, NOT_COMPILED, UNKNOWN = (
    "solved", "infeasible", "stopped", "unbounded", "bound_violation",
    "no_solution", "not_compiled", "unknown")

STATUS_HELP = {
    SOLVED: "CBC found an optimal solution and the results were collected.",
    INFEASIBLE: "CBC proved the LP infeasible: no solution satisfies every constraint.",
    STOPPED: "CBC stopped before finishing (time or iteration limit); the LP may be feasible.",
    UNBOUNDED: "CBC found the LP unbounded (a cost can fall without limit).",
    VIOLATION: "CBC reported optimal, but a variable breaks its bounds; OSTRAM rejected the solution.",
    NO_SOLUTION: "The model was built (input CSV exists) but no solution file was found: "
                 "the solve did not run, crashed, or the folder was cleared.",
    NOT_COMPILED: "The future was never built: no input CSV, no results.",
    UNKNOWN: "Not solved; the solution file is not reachable from this computer, so the "
             "exact solver status is unknown.",
}


def read_sol_header(path: Path) -> tuple[str, str]:
    """Return (status class, first line) of a CBC/GLPK solution file."""

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = next((ln.strip() for ln in fh if ln.strip()), "")
            violation = any(ln.lstrip().startswith("**") and _bad_violation(ln) for ln in fh)
    except OSError:
        return UNKNOWN, ""
    low = first.lower()
    if low.startswith("optimal"):
        return (VIOLATION if violation else SOLVED), first
    if "infeasible" in low:
        return INFEASIBLE, first
    if "unbounded" in low:
        return UNBOUNDED, first
    if low.startswith("stopped") or "time" in low or "iterations" in low:
        return STOPPED, first
    return UNKNOWN, first


def _bad_violation(line: str) -> bool:
    try:
        value = float(line.split()[3])
    except (IndexError, ValueError):
        return True
    return not -1e-6 <= value <= 0


def future_folders(run: RunRef, status_row: dict) -> list[Path]:
    """Where B2 may have left this future's files, most likely first."""

    out = []
    for col in ("Input_CSV", "Output_CSV"):
        p = _text(status_row.get(col))
        if p:
            out.append(Path(p).parent)
    fid = status_row["Future_ID"]
    # <workspace>[/setups/<setup>]/execution/Executables/<future>_0
    out.append(run.path.parents[1] / "execution" / "Executables" / f"{fid}_0")
    return out


def solver_status(run: RunRef, status: pd.DataFrame) -> pd.DataFrame:
    """Add Solver_Status / Solver_Message / Sol_File to RDM_Run_Status rows."""

    rows = []
    for rec in status.to_dict("records"):
        st = _text(rec.get("Status")).lower()
        cls, message, sol = UNKNOWN, "", ""
        folders = [f for f in future_folders(run, rec) if f.is_dir()]
        sols = []
        for f in folders:
            sols = sorted(p for p in f.glob("*.sol") if "feasopt" not in p.name)
            if sols:
                break
        if sols:
            sol = str(sols[0])
            cls, message = read_sol_header(sols[0])
            if st == "solved" and cls == UNKNOWN:
                cls = SOLVED
        elif st == "solved":
            cls = SOLVED
        elif st == "missing":
            cls = NOT_COMPILED
        elif folders:
            cls = NO_SOLUTION
        else:
            cls = UNKNOWN
        if st == "solved" and cls != SOLVED:
            # results were collected, so the solve passed OSTRAM's checks
            cls = SOLVED
        rows.append({**rec, "Solver_Status": cls, "Solver_Message": message, "Sol_File": sol})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# The run's data
# ---------------------------------------------------------------------------
@dataclass
class RunData:
    ref: RunRef
    manifest: dict
    interface: Interface
    design: pd.DataFrame
    status: pd.DataFrame            # one row per future, with Solver_Status
    inputs_path: Path | None
    outputs_path: Path | None
    warnings: list[str] = field(default_factory=list)
    _inputs_cache: dict = field(default_factory=dict)

    # -- futures -----------------------------------------------------------
    @property
    def futures(self) -> list[str]:
        return list(self.design["Future_ID"])

    def named(self, future: str) -> str:
        row = self.design.loc[self.design.Future_ID == future]
        return str(row.Named_Future.iloc[0]) if len(row) else future

    def sample(self, future: str) -> int:
        row = self.design.loc[self.design.Future_ID == future]
        return int(row.Sample.iloc[0]) if len(row) else 0

    def status_of(self, future: str) -> str:
        row = self.status.loc[self.status.Future_ID == future]
        return str(row.Solver_Status.iloc[0]) if len(row) else UNKNOWN

    @property
    def solved(self) -> list[str]:
        return [f for f in self.futures if self.status_of(f) == SOLVED]

    @property
    def unsolved(self) -> list[str]:
        return [f for f in self.futures if self.status_of(f) != SOLVED]

    def reference_future(self, preferred: str | None = None) -> str:
        """The future others are compared with: F00 if present, else the first central future."""

        if preferred and preferred in self.futures:
            return preferred
        if "F00" in self.futures:
            return "F00"
        central = [f for f in self.futures if self.sample(f) == 0]
        return central[0] if central else self.futures[0]

    def central_of(self, future: str) -> str | None:
        """The future's own central future (same named future, sample 0), if it was run."""
        c = self.named(future)
        return c if c in self.futures and c != future else None

    # -- input values ------------------------------------------------------
    def input_columns(self) -> list[str]:
        if not self.inputs_path:
            return []
        return list(pd.read_csv(self.inputs_path, nrows=0).columns)

    def input_values(self, parameters: list[str]) -> pd.DataFrame:
        """Long table: ID columns + set columns + one column per requested parameter.

        Parameters that are not in RDM_Inputs.csv (Export = N on the Outputs
        sheet) are read from each future's own B2 input CSV when this
        computer can reach it.
        """

        parameters = list(dict.fromkeys(p for p in parameters if p))
        key = tuple(sorted(parameters))
        if key in self._inputs_cache:
            return self._inputs_cache[key]

        have = set(self.input_columns())
        from_rdm = [p for p in parameters if p in have]
        missing = [p for p in parameters if p not in have]
        frames = []
        if from_rdm:
            cols = [c for c in ID_COLUMNS + SET_COLUMNS if c in have] + from_rdm
            df = pd.read_csv(self.inputs_path, usecols=cols, low_memory=False)
            frames.append(df.dropna(subset=from_rdm, how="all"))
        if missing:
            extra = self._from_future_csvs(missing)
            if extra is not None:
                frames.append(extra)
            got = set(extra.columns) if extra is not None else set()
            lost = [p for p in missing if p not in got]
            for p in lost:
                msg = (f"{p}: not in RDM_Inputs.csv (Export = N on the Outputs sheet) and the "
                       "futures' own input CSVs are not reachable; set Export = Y and re-collect.")
                if msg not in self.warnings:
                    self.warnings.append(msg)
        if not frames:
            out = pd.DataFrame(columns=ID_COLUMNS + SET_COLUMNS + parameters)
        else:
            out = pd.concat(frames, ignore_index=True)
        for c in ID_COLUMNS + SET_COLUMNS + parameters:
            if c not in out.columns:
                out[c] = np.nan
        for c in KEY_COLUMNS:
            out[c] = out[c].map(_norm_key)
        out["YEAR"] = pd.to_numeric(out["YEAR"], errors="coerce")
        out["Future_ID"] = out["Future_ID"].astype(str)
        self._inputs_cache[key] = out
        return out

    def _from_future_csvs(self, parameters: list[str]) -> pd.DataFrame | None:
        frames = []
        for rec in self.status.to_dict("records"):
            p = _text(rec.get("Input_CSV"))
            if not p or not Path(p).is_file():
                continue
            header = pd.read_csv(p, nrows=0).columns
            cols = [c for c in parameters if c in header]
            if not cols:
                continue
            sets = [c for c in SET_COLUMNS if c in header]
            df = pd.read_csv(p, usecols=sets + cols, low_memory=False).dropna(subset=cols, how="all")
            df.insert(0, "Future_ID", rec["Future_ID"])
            df.insert(1, "Named_Future", self.named(rec["Future_ID"]))
            df.insert(2, "Sample", self.sample(rec["Future_ID"]))
            frames.append(df)
        if not frames:
            return None
        note = f"Read {', '.join(parameters)} from each future's own input CSV (not exported to RDM_Inputs.csv)."
        if note not in self.warnings:
            self.warnings.append(note)
        return pd.concat(frames, ignore_index=True)

    def output_totals(self) -> pd.DataFrame:
        """TotalDiscountedCost per solved future, when exported."""
        if not self.outputs_path:
            return pd.DataFrame(columns=["Future_ID", "TotalDiscountedCost"])
        cols = pd.read_csv(self.outputs_path, nrows=0).columns
        if "TotalDiscountedCost" not in cols:
            return pd.DataFrame(columns=["Future_ID", "TotalDiscountedCost"])
        df = pd.read_csv(self.outputs_path, usecols=["Future_ID", "TotalDiscountedCost"], low_memory=False)
        return df.groupby("Future_ID", as_index=False)["TotalDiscountedCost"].sum()

    def own_csv(self, future: str, kind: str = "Input") -> Path | None:
        """The future's own B2 input or output CSV, when this computer can reach it."""
        row = self.status.loc[self.status.Future_ID == future]
        if not len(row):
            return None
        p = _text(row[f"{kind}_CSV"].iloc[0]) if f"{kind}_CSV" in row.columns else ""
        if p and Path(p).is_file():
            return Path(p)
        if kind == "Input":   # B2's default location, in case the status file has no path
            for folder in future_folders(self.ref, row.iloc[0].to_dict()):
                cand = folder / f"{future}_0_Input.csv"
                if cand.is_file():
                    return cand
        return None

    # -- output folder -----------------------------------------------------
    @property
    def out_dir(self) -> Path:
        d = self.ref.path / "diagnosis"
        d.mkdir(exist_ok=True)
        return d


def _norm_key(v) -> str:
    """1, 1.0 and '1' compare equal; blanks become ''."""
    s = _text(v)
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except ValueError:
        pass
    return s


def load_run(ref: RunRef) -> RunData:
    run_dir = ref.path
    manifest = json.loads((run_dir / "rdm_manifest.json").read_text(encoding="utf-8"))
    iface_path = run_dir / "interface.xlsx"
    if not iface_path.is_file():
        raise SystemExit(f"{iface_path} is missing; the run folder is incomplete.")
    interface = read_interface(iface_path)

    design_path = run_dir / "RDM_Experiment_Design.csv"
    if design_path.is_file():
        design = pd.read_csv(design_path, dtype={"Future_ID": str, "Named_Future": str})
    else:
        design = pd.DataFrame({"Future_ID": manifest["futures"], "Named_Future": manifest["futures"],
                               "Sample": 0})
    design["Sample"] = pd.to_numeric(design["Sample"], errors="coerce").fillna(0).astype(int)

    warnings = []
    status_path = run_dir / "RDM_Run_Status.csv"
    if status_path.is_file():
        status = pd.read_csv(status_path, dtype=str).fillna("")
    else:
        warnings.append("RDM_Run_Status.csv not found: the run has not been collected yet. "
                        "Run 'python -m ostram rdm collect ...' first; every future is treated as unknown.")
        status = pd.DataFrame({"Future_ID": design.Future_ID, "Named_Future": design.Named_Future,
                               "Sample": design.Sample.astype(str), "Status": "",
                               "Input_CSV": "", "Output_CSV": ""})
    # futures in the design but absent from the status file
    for fid in design.Future_ID:
        if fid not in set(status.Future_ID):
            status = pd.concat([status, pd.DataFrame([{"Future_ID": fid, "Status": "missing"}])],
                               ignore_index=True)
    status = solver_status(ref, status.fillna(""))
    order = {f: i for i, f in enumerate(design.Future_ID)}
    status = status.sort_values("Future_ID", key=lambda s: s.map(order)).reset_index(drop=True)

    inputs = run_dir / "RDM_Inputs.csv"
    outputs = run_dir / "RDM_Outputs.csv"
    if not inputs.is_file():
        warnings.append("RDM_Inputs.csv not found; perturbed values can only be read from the "
                        "futures' own input CSVs.")
    return RunData(ref, manifest, interface, design, status,
                   inputs if inputs.is_file() else None,
                   outputs if outputs.is_file() else None, warnings)
