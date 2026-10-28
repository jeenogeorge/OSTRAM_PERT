# -*- coding: utf-8 -*-
"""
run_diagnosis.py — look inside an OSTRAM RDM run, outside the pipeline.

Pick a run (test01, test02, ...) and this script writes into that run's own
folder, ``<run>/diagnosis/``:

  Perturbed_Inputs.xlsx            how every lever perturbed the inputs, future by future
  Perturbed_Inputs_Dashboard.html  the same as interactive charts (open in a browser)
  Inputs_by_Set.xlsx               every input that differs between futures, year by year,
                                   listed by Technology / Commodity / Emission / UDC /
                                   Storage / Region
  Inputs_by_Set_all_years.csv      every input value of every future, year by year
  Outputs_by_Set.xlsx (+ .csv)     the same for the results of the solved futures
  Unsolved_Futures.csv             the futures that did not solve, and what the solver said
  Infeasibility_Diagnosis.xlsx     for each unsolved future: is a perturbed parameter the cause?

It only reads the run folder (and, when it can reach them, each future's own
B2 files); it never changes the run and is not part of ``python -m ostram``.

Spyder: edit CONFIG below and press F5.
Prompt:  python diagnosis_for_rdm/run_diagnosis.py --run test01
         python diagnosis_for_rdm/run_diagnosis.py --list
         python diagnosis_for_rdm/run_diagnosis.py --run test02 --setup unescap_training
         python diagnosis_for_rdm/run_diagnosis.py --run-dir D:/OSTRAM/workspace/setups/unescap_training/rdm/test01
Flags override CONFIG.  ``python run_diagnosis.py --help`` lists them all.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from pathlib import Path

# =============================================================================
# CONFIG
# =============================================================================
CONFIG = {
    # Which run. None = ask (lists every run found, newest first).
    # "test 01", "test_01" and "test01" all find the folder test01.
    "run": None,
    # Only needed when two setups have a run of the same name, e.g. "unescap_training".
    "setup": None,
    # The OSTRAM workspace. None = OSTRAM_WORKSPACE if set, else <this clone>/workspace.
    "workspace": None,
    # Or point straight at a run folder (overrides run / setup / workspace).
    "run_dir": None,

    # What to make. Any of: "perturbed", "sets", "outputs", "infeasibility", "dashboard"
    "steps": ["perturbed", "sets", "outputs", "infeasibility", "dashboard"],
    # Futures to diagnose. None = every future that did not solve.
    "futures": None,
    # Future every other future is compared with. None = F00, else the first central future.
    "reference": None,
    # Years shown in Perturbed_Inputs.xlsx: "all", "5" (every 5th year), "10", or a list.
    # The by-set workbooks always show every year.
    "years": "all",
    # Include timeslice-level parameters (CapacityFactor, YearSplit ...) in the by-set listing.
    "include_timeslice": False,
    "open_dashboard": True,
}
# =============================================================================

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ALL_STEPS = ("perturbed", "sets", "outputs", "infeasibility", "dashboard")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=CONFIG["run"], help="Run name, e.g. test01")
    ap.add_argument("--setup", default=CONFIG["setup"])
    ap.add_argument("--workspace", default=CONFIG["workspace"])
    ap.add_argument("--run-dir", default=CONFIG["run_dir"])
    ap.add_argument("--list", action="store_true", help="List the runs and their unsolved futures, then stop.")
    ap.add_argument("--steps", default=",".join(CONFIG["steps"]),
                    help=f"Comma list of {', '.join(ALL_STEPS)} (default: all)")
    ap.add_argument("--futures", nargs="*", default=CONFIG["futures"],
                    help="Futures to diagnose (default: every unsolved future)")
    ap.add_argument("--reference", default=CONFIG["reference"])
    ap.add_argument("--years", default=CONFIG["years"])
    ap.add_argument("--timeslice", action="store_true", default=CONFIG["include_timeslice"])
    ap.add_argument("--no-open", action="store_true", default=not CONFIG["open_dashboard"])
    # Spyder passes nothing; ignore stray IPython arguments
    args, _unknown = ap.parse_known_args(argv)
    args.steps = [s.strip().lower() for s in args.steps.split(",") if s.strip()]
    bad = [s for s in args.steps if s not in ALL_STEPS]
    if bad:
        ap.error(f"unknown step(s) {bad}; choose from {', '.join(ALL_STEPS)}")
    return args


def pick_years(spec, all_years):
    from rdmdiag.levers import default_years
    ys = sorted({int(y) for y in all_years if y == y})
    if spec in (None, "", "all"):
        return ys
    if isinstance(spec, (list, tuple)):
        return [int(y) for y in spec]
    s = str(spec).strip()
    if s in ("5", "10"):
        step = int(s)
        return sorted({ys[0], ys[-1]} | {y for y in ys if y % step == 0}) if ys else []
    if "," in s:
        return [int(y) for y in s.split(",") if y.strip()]
    return default_years(ys)


def list_runs(workspace):
    from rdmdiag.run import find_runs, load_run, default_workspace
    runs = find_runs(workspace)
    if not runs:
        print(f"No RDM runs under {workspace or default_workspace()}")
        return
    for r in runs:
        try:
            d = load_run(r)
            un = d.unsolved
            print(f"  {r.label:40s} {len(d.futures):>3} futures, {len(d.solved):>3} solved, "
                  f"unsolved: {', '.join(f'{f} ({d.status_of(f)})' for f in un) or 'none'}")
        except SystemExit as e:
            print(f"  {r.label:40s} (cannot read: {e})")


def main(argv=None):
    args = parse_args(argv)
    from rdmdiag.run import resolve_run, load_run, SOLVED
    if args.list:
        list_runs(args.workspace)
        return 0

    t0 = time.time()
    ref = resolve_run(args.run, args.setup, args.run_dir, args.workspace)
    print(f"\nRun: {ref.label}\n  {ref.path}")
    data = load_run(ref)
    out = data.out_dir
    for w in data.warnings:
        print(f"  note: {w}")

    # -- which futures did not solve ------------------------------------------
    from rdmdiag import infeasibility as inf
    unsolved = inf.unsolved_table(data)
    unsolved.to_csv(out / "Unsolved_Futures.csv", index=False)
    print(f"\nFutures: {len(data.futures)}  ·  solved: {len(data.solved)}  ·  not solved: {len(data.unsolved)}")
    for r in unsolved.itertuples():
        print(f"  ✕ {r.Future_ID:14s} {r.Solver_Status:15s} {r.Solver_Message}")
    written = [out / "Unsolved_Futures.csv"]

    need_p = any(s in args.steps for s in ("perturbed", "infeasibility", "dashboard"))
    need_sets = any(s in args.steps for s in ("sets", "infeasibility", "dashboard"))

    p = listing = dx = None
    if need_p:
        from rdmdiag import perturbed
        print("\nReading how the levers perturbed the inputs ...")
        params = sorted({lv.parameter for lv in data.interface.levers if lv.parameter})
        yrs = pick_years(args.years, data.input_values(params)["YEAR"].unique())
        p = perturbed.analyse(data, years=yrs, reference=args.reference)
        if "perturbed" in args.steps:
            written.append(perturbed.write_excel(data, p, out / "Perturbed_Inputs.xlsx"))
        flagged = p.checks[p.checks.Check.ne("")] if len(p.checks) else p.checks
        print(f"  {len(p.levers)} levers · {len(flagged)} thing(s) that look off")

    if need_sets:
        from rdmdiag import allsets
        print("Listing every input by set, year by year ...")
        listing = allsets.list_by_set(data, "Input", reference=args.reference,
                                      include_timeslice=args.timeslice)
        if "sets" in args.steps:
            written.append(allsets.write_excel(data, listing, out / "Inputs_by_Set.xlsx"))
            if listing.all_csv:
                written.append(listing.all_csv)
        if len(listing.summary):
            moved = listing.summary[listing.summary.Rows_That_Differ > 0]
            print(f"  {len(listing.summary)} parameters · {len(moved)} differ between futures: "
                  + ", ".join(moved.Parameter.head(8)) + (" ..." if len(moved) > 8 else ""))
        for n in listing.notes:
            print(f"  note: {n}")

    if "outputs" in args.steps:
        from rdmdiag import allsets
        print("Listing every result by set, year by year ...")
        ol = allsets.list_by_set(data, "Output", reference=args.reference,
                                 include_timeslice=args.timeslice)
        if ol.varying is not None and len(ol.summary):
            written.append(allsets.write_excel(data, ol, out / "Outputs_by_Set.xlsx"))
            if ol.all_csv:
                written.append(ol.all_csv)
        else:
            print("  no results to list")

    if "infeasibility" in args.steps and not data.solved:
        print("\nNo future is marked solved, so there is nothing to compare the unsolved ones with.\n"
              "  If the run finished, collect it first:  python -m ostram [--setup <setup>] rdm collect "
              f"--run {ref.name}")
    elif "infeasibility" in args.steps:
        print(f"\nDiagnosing {', '.join(args.futures)} ..." if args.futures
              else "\nDiagnosing the futures that did not solve ...")
        dx = inf.diagnose(data, p, listing, futures=args.futures)
        written.append(inf.write_excel(data, dx, out / "Infeasibility_Diagnosis.xlsx"))
        inf.print_report(data, dx)

    if "dashboard" in args.steps:
        from rdmdiag.dashboard import build_dashboard
        html_path = build_dashboard(data, p, out / "Perturbed_Inputs_Dashboard.html", listing, dx)
        written.append(html_path)
        if not args.no_open:
            webbrowser.open(html_path.resolve().as_uri())

    print(f"\nWritten to {out}  ({time.time() - t0:.0f} s):")
    for w in written:
        print(f"  {Path(w).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
