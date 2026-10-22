"""Expand an RDM interface into futures and write the run folder.

Each named future becomes one derived scenario.  When the interface asks for
samples, the ranged uncertainties that apply to a future are layered inside
it: ``F01`` becomes ``F01_s001`` ... ``F01_sNNN``, each with the future's own
changes followed by one Latin Hypercube draw of the uncertainties.  Sample
``s`` uses the same draw in every future (common random numbers), so a
difference between ``F01_s007`` and ``F02_s007`` comes from the futures'
changes, not from the sampling.

The run folder holds:

* ``futures.yaml``: the ``ostram-futures-v1`` list the registry reads
  through ``OSTRAM_RDM_FUTURES``;
* ``scenarios/<future>/scenario.yaml``: each future as an ordinary derived
  scenario, in the format of ``config/scenarios/<name>/scenario.yaml``;
* ``RDM_Experiment_Design.csv``: one row per future with its sampled values;
* ``interface.xlsx``: a copy of the workbook the run came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import csv
import json
from pathlib import Path
import shutil
from typing import Sequence

import numpy as np
import yaml

from ostram.pipeline.scenarios.registry import FUTURES_SCHEMA
from ostram.rdm.interface import Change, Interface

_SELECTOR_KEYS = {
    "Tech": "tech", "Techs": "techs", "Tech_Prefix": "tech_prefix",
    "Fuel": "fuel", "Fuels": "fuels", "Fuel_Prefix": "fuel_prefix",
}


@dataclass
class FutureSpec:
    """One future to run: its scenario name, origin and edits."""

    name: str
    named_future: str
    sample: int  # 0 for the central future (no sampled uncertainties)
    edits: list[dict]
    sampled: dict[str, float] = field(default_factory=dict)


def latin_hypercube(samples: int, dimensions: int, rng: np.random.Generator) -> np.ndarray:
    """Return a ``samples`` x ``dimensions`` Latin Hypercube design on [0, 1).

    Each column has exactly one point in each of the ``samples`` equal strata.
    """

    if samples <= 0 or dimensions <= 0:
        return np.zeros((max(samples, 0), max(dimensions, 0)))
    design = np.empty((samples, dimensions))
    for column in range(dimensions):
        strata = rng.permutation(samples)
        design[:, column] = (strata + rng.random(samples)) / samples
    return design


def _years(first: int, last: int) -> list[int]:
    return list(range(first, last + 1))


def change_to_edits(change: Change, first_year: int, last_year: int) -> list[dict]:
    """Translate one interface change into scenario edits for ``scenario_edits``."""

    base: dict = {"sheet": change.sheet, "param": change.parameter}
    key = _SELECTOR_KEYS[change.selector_type]
    if key in ("techs", "fuels"):
        base[key] = [part.strip() for part in change.selector.split(",") if part.strip()]
    else:
        base[key] = change.selector
    note = change.note or ""
    if change.where:
        note = f"{note} [{change.where}]".strip()
    if note:
        base["note"] = note

    limited = change.year_from is not None or change.year_to is not None
    start = change.year_from if change.year_from is not None else first_year
    end = change.year_to if change.year_to is not None else last_year

    if change.operation == "Multiply":
        edit = {**base, "op": "multiply", "factor": round(float(change.value), 6)}
        if limited:
            edit["years"] = _years(start, end)
        return [edit]
    if change.operation == "Set":
        value = round(float(change.value), 6)
        if limited:
            return [{**base, "values": {str(y): value for y in _years(start, end)}}]
        return [{**base, "op": "set_flat", "value": value}]
    if change.operation == "Copy_From_Tech":
        return [{**base, "op": "copy_from_tech", "source_tech": str(change.value)}]
    if change.operation == "Multiply_Ramp":
        target = float(change.value)
        span = change.year_to - change.year_from
        edits = []
        for year in range(change.year_from + 1, change.year_to + 1):
            factor = round(1 + (target - 1) * (year - change.year_from) / span, 6)
            edits.append({**base, "op": "multiply", "factor": factor, "years": [year]})
        if change.year_to < last_year:
            edits.append({
                **base, "op": "multiply", "factor": round(target, 6),
                "years": _years(change.year_to + 1, last_year),
            })
        return edits
    raise ValueError(f"unknown operation {change.operation!r}")


def sample_values(interface: Interface) -> tuple[list[str], np.ndarray]:
    """Return the uncertainty ids and a samples x uncertainties array of values.

    A linked uncertainty takes the same position in its own range as the
    uncertainty it names.
    """

    uncertainties = interface.uncertainties
    ids = [u.x_id for u in uncertainties]
    n = interface.samples_per_future
    if not uncertainties or n == 0:
        return ids, np.zeros((n, len(ids)))
    independent = [u.x_id for u in uncertainties if u.linked_to is None]
    rng = np.random.default_rng(interface.random_seed)
    design = latin_hypercube(n, len(independent), rng)
    column = {x_id: i for i, x_id in enumerate(independent)}
    values = np.empty((n, len(uncertainties)))
    for j, u in enumerate(uncertainties):
        position = design[:, column[u.linked_to or u.x_id]]
        values[:, j] = np.round(u.minimum + position * (u.maximum - u.minimum), 6)
    return ids, values


def expand_futures(interface: Interface) -> list[FutureSpec]:
    """Return every future to run, in Futures-sheet order, samples after their central future."""

    first, last = interface.model_first_year, interface.model_last_year
    ids, values = sample_values(interface)
    n = interface.samples_per_future
    width = max(3, len(str(n)))
    specs: list[FutureSpec] = []
    for future in interface.active_futures:
        own = [
            edit
            for change in interface.changes.get(future.future_id, [])
            for edit in change_to_edits(change, first, last)
        ]
        applicable = [
            (j, u) for j, u in enumerate(interface.uncertainties)
            if u.applies_to is None or future.future_id in u.applies_to
        ]
        if n == 0 or not applicable:
            specs.append(FutureSpec(future.future_id, future.future_id, 0, own))
            continue
        if interface.run_central_future:
            specs.append(FutureSpec(future.future_id, future.future_id, 0, own))
        for s in range(n):
            sampled = {u.x_id: float(values[s, j]) for j, u in applicable}
            layered = [
                edit
                for j, u in applicable
                for edit in change_to_edits(u.change(float(values[s, j])), first, last)
            ]
            specs.append(FutureSpec(
                f"{future.future_id}_s{s + 1:0{width}d}", future.future_id, s + 1,
                own + layered, sampled,
            ))
    return specs


def _describe(interface: Interface, spec: FutureSpec) -> str:
    future = next(f for f in interface.futures if f.future_id == spec.named_future)
    text = f"RDM run {interface.run_name}: future {future.future_id}"
    if future.name:
        text += f" ({future.name})"
    if spec.sample:
        draws = ", ".join(f"{k}={v:g}" for k, v in spec.sampled.items())
        text += f", sample {spec.sample}: {draws}"
    return text + ". Generated by ostram rdm; edit the interface workbook, not this file."


def write_run_folder(
    interface: Interface,
    run_dir: Path | str,
    *,
    apply_vre_ceilings: bool,
    reserved_names: Sequence[str] = (),
    overwrite: bool = False,
) -> list[FutureSpec]:
    """Write the run folder for ``interface`` and return the futures it holds.

    ``reserved_names`` are scenario names already in the registry; a future
    may not reuse one.  An existing run folder is replaced only with
    ``overwrite``.
    """

    run_dir = Path(run_dir)
    specs = expand_futures(interface)
    clashes = sorted({s.name for s in specs} & set(reserved_names))
    if clashes:
        raise ValueError(
            f"future name(s) {', '.join(clashes)} are already registered scenarios; "
            "choose other Future_IDs"
        )
    if run_dir.exists():
        if not overwrite:
            raise FileExistsError(
                f"run folder exists: {run_dir}. Change Setup > Run_Name or pass --overwrite."
            )
        shutil.rmtree(run_dir)
    scenarios_dir = run_dir / "scenarios"
    scenarios_dir.mkdir(parents=True)

    for spec in specs:
        document = {
            "scenario": spec.name,
            "base_scenario": interface.base_scenario,
            "description": _describe(interface, spec),
            "apply_vre_ceiling_layer": bool(apply_vre_ceilings),
            "edits": spec.edits,
        }
        folder = scenarios_dir / spec.name
        folder.mkdir()
        (folder / "scenario.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100, default_flow_style=None),
            encoding="utf-8",
        )

    futures_doc = {
        "schema": FUTURES_SCHEMA,
        "run_name": interface.run_name,
        "base_scenario": interface.base_scenario,
        "futures": [spec.name for spec in specs],
    }
    (run_dir / "futures.yaml").write_text(
        yaml.safe_dump(futures_doc, sort_keys=False), encoding="utf-8"
    )

    ids = [u.x_id for u in interface.uncertainties]
    names = {f.future_id: f.name for f in interface.futures}
    with (run_dir / "RDM_Experiment_Design.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Future_ID", "Named_Future", "Future_Name", "Sample", *ids])
        for spec in specs:
            writer.writerow([
                spec.name, spec.named_future, names.get(spec.named_future, ""), spec.sample,
                *[spec.sampled.get(x, "") for x in ids],
            ])

    shutil.copyfile(interface.path, run_dir / "interface.xlsx")
    (run_dir / "rdm_manifest.json").write_text(json.dumps({
        "run_name": interface.run_name,
        "base_scenario": interface.base_scenario,
        "samples_per_future": interface.samples_per_future,
        "random_seed": interface.random_seed,
        "run_central_future": interface.run_central_future,
        "apply_vre_ceilings": bool(apply_vre_ceilings),
        "futures": [spec.name for spec in specs],
        "input_variables": interface.input_variables,
        "output_variables": interface.output_variables,
    }, indent=2), encoding="utf-8")
    return specs
