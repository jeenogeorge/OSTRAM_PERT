"""Collect the solved futures of an RDM run into its CSVs.

B2 leaves each future's inputs and results in
``<execution workspace>/Executables/<future>_0/``.  This module reads them one
future at a time, keeps the set columns and the variables chosen on the
Outputs sheet, puts ``Future_ID``, ``Named_Future`` and ``Sample`` in front,
and appends them to ``RDM_Inputs.csv`` and ``RDM_Outputs.csv`` in the run
folder.  ``RDM_Run_Status.csv`` records what was found for each future.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
from pathlib import Path
from typing import Sequence

import pandas as pd
import yaml

from ostram.pipeline.execution.scenario_concatenation import (
    KEY_SET_COLUMNS,
    active_output_csv_candidates,
)
from ostram.paths import resolve_paths

ID_COLUMNS = ["Future_ID", "Named_Future", "Sample"]


def execution_params(execution_workspace: Path) -> dict:
    """Return the B2 parameters the way B2 resolves them for this workspace."""

    local = execution_workspace / "Config_MOMF_T1_AB.yaml"
    project = resolve_paths()
    path = local if local.is_file() else project.execution_config
    params = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not local.is_file():
        # As in B2's run plan: the active setup's folders replace the
        # config's path entries, which may hold ${...} manifest tokens.
        params["executables"] = str(project.executables)
    from ostram.pipeline.execution.orchestrator import apply_configuration_overrides
    with contextlib.redirect_stdout(io.StringIO()):  # it reports each override
        apply_configuration_overrides(params)
    return params


def future_files(execution_workspace: Path, params: dict, future: str) -> tuple[Path | None, Path | None]:
    """Return the (input CSV, output CSV) of ``future``; either is None when absent."""

    name = f"{future}_0"
    folder = Path(params.get("executables", "Executables"))
    if not folder.is_absolute():
        folder = execution_workspace / folder
    folder = folder / name
    input_path = folder / f"{name}_Input.csv"
    output_path = next(
        (folder / candidate for candidate in active_output_csv_candidates(params, name)
         if (folder / candidate).is_file()),
        None,
    )
    return (input_path if input_path.is_file() else None), output_path


def _select(frame: pd.DataFrame, variables: Sequence[str], design_row: dict) -> pd.DataFrame:
    keep_vars = [v for v in variables if v in frame.columns]
    if not keep_vars:
        return pd.DataFrame()
    frame = frame.dropna(subset=keep_vars, how="all")
    sets = [c for c in KEY_SET_COLUMNS if c in frame.columns and frame[c].notna().any()]
    frame = frame[sets + keep_vars].copy()
    for position, column in enumerate(ID_COLUMNS):
        frame.insert(position, column, design_row[column])
    return frame


class _Appender:
    """Append blocks to one CSV, widening nothing: the columns come from the first block."""

    def __init__(self, path: Path, columns: list[str]):
        self.path, self.columns, self.started = path, columns, False

    def append(self, block: pd.DataFrame) -> None:
        block = block.reindex(columns=self.columns)
        block.to_csv(self.path, index=False, mode="a" if self.started else "w", header=not self.started)
        self.started = True


def _columns(variables: Sequence[str], present: set[str]) -> list[str]:
    sets = [c for c in KEY_SET_COLUMNS if c in present]
    return ID_COLUMNS + sets + [v for v in variables if v in present]


def collect_run(run_dir: Path | str, execution_workspace: Path | str | None = None) -> dict:
    """Write ``RDM_Inputs.csv``, ``RDM_Outputs.csv`` and ``RDM_Run_Status.csv``.

    Returns a summary: the paths written and the futures solved, compiled
    only, or missing.
    """

    run_dir = Path(run_dir)
    workspace = Path(execution_workspace) if execution_workspace else resolve_paths().execution_workspace
    manifest = json.loads((run_dir / "rdm_manifest.json").read_text(encoding="utf-8"))
    with (run_dir / "RDM_Experiment_Design.csv").open(encoding="utf-8") as stream:
        design = {row["Future_ID"]: row for row in csv.DictReader(stream)}
    params = execution_params(workspace)
    inputs_vars = manifest.get("input_variables", [])
    outputs_vars = manifest.get("output_variables", [])

    found = {future: future_files(workspace, params, future) for future in manifest["futures"]}

    # Pass 1: headers only, so every block shares one column layout.
    in_present, out_present = set(), set()
    for input_path, output_path in found.values():
        if input_path is not None:
            in_present.update(pd.read_csv(input_path, nrows=0).columns)
        if output_path is not None:
            out_present.update(pd.read_csv(output_path, nrows=0).columns)

    in_path, out_path = run_dir / "RDM_Inputs.csv", run_dir / "RDM_Outputs.csv"
    for stale in (in_path, out_path):
        stale.unlink(missing_ok=True)
    in_writer = _Appender(in_path, _columns(inputs_vars, in_present)) if in_present else None
    out_writer = _Appender(out_path, _columns(outputs_vars, out_present)) if out_present else None

    status_rows = []
    for future, (input_path, output_path) in found.items():
        row = design.get(future, {"Future_ID": future, "Named_Future": future, "Sample": 0})
        if input_path is not None and in_writer is not None:
            in_writer.append(_select(pd.read_csv(input_path, low_memory=False), inputs_vars, row))
        if output_path is not None and out_writer is not None:
            out_writer.append(_select(pd.read_csv(output_path, low_memory=False), outputs_vars, row))
        status = "solved" if output_path else ("compiled" if input_path else "missing")
        status_rows.append({
            "Future_ID": future, "Named_Future": row["Named_Future"], "Sample": row["Sample"],
            "Status": status,
            "Input_CSV": str(input_path or ""), "Output_CSV": str(output_path or ""),
        })

    status_path = run_dir / "RDM_Run_Status.csv"
    with status_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(status_rows[0]) if status_rows else ["Future_ID"])
        writer.writeheader()
        writer.writerows(status_rows)

    summary = {
        "inputs": str(in_path) if in_writer and in_writer.started else None,
        "outputs": str(out_path) if out_writer and out_writer.started else None,
        "status": str(status_path),
        "solved": [r["Future_ID"] for r in status_rows if r["Status"] == "solved"],
        "compiled": [r["Future_ID"] for r in status_rows if r["Status"] == "compiled"],
        "missing": [r["Future_ID"] for r in status_rows if r["Status"] == "missing"],
    }
    return summary
