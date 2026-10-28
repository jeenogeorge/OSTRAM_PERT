# diagnosis_for_rdm

Look inside a finished OSTRAM RDM run: how the inputs were perturbed, every
input listed by set and year, which futures did not solve, and whether a
perturbed parameter is the cause.

This folder is **not part of the OSTRAM pipeline**. Nothing in `ostram/`
imports it and `python -m ostram` never runs it. It only reads a run folder
(and each future's own B2 files when it can reach them), and writes into that
run's `diagnosis/` subfolder. Run it whenever you like, on any run, as often
as you like.

## Run it

**Spyder:** open `run_diagnosis.py`, set `"run"` in the `CONFIG` block (for
example `"test01"`), press F5. Leave `"run": None` to be asked which run.

**Prompt** (from the clone root, in the OSTRAM conda env):

```bat
python diagnosis_for_rdm\run_diagnosis.py --list
python diagnosis_for_rdm\run_diagnosis.py --run test01
python diagnosis_for_rdm\run_diagnosis.py --run test02 --setup unescap_training
python diagnosis_for_rdm\run_diagnosis.py --run-dir D:\OSTRAM\workspace\setups\unescap_training\rdm\test01
```

`--list` shows every run in the workspace with its unsolved futures.
`test 01`, `test_01` and `test01` all find the folder `test01`. Runs are
looked for in `<workspace>\rdm\` (South Asia) and
`<workspace>\setups\<setup>\rdm\` (e.g. `unescap_training`); add `--setup`
only if two setups have a run of the same name. The workspace is
`OSTRAM_WORKSPACE` if set, else `workspace\` in this clone; `--workspace`
points elsewhere.

| Option | Default | Meaning |
|---|---|---|
| `--steps` | all | Any of `perturbed, sets, outputs, infeasibility, dashboard` |
| `--futures F00_s002 F01_s004` | every unsolved future | Futures to diagnose (a solved one can be named too) |
| `--reference F01` | `F00` | Future the others are compared with |
| `--years all \| 5 \| 10 \| 2030,2050` | `all` | Year columns of `Perturbed_Inputs.xlsx` (the by-set files always show every year) |
| `--timeslice` | off | Also list timeslice parameters (CapacityFactor, YearSplit ...) |
| `--no-open` | off | Do not open the dashboard in the browser |

## What it writes, in `<run>\diagnosis\`

| File | What it shows |
|---|---|
| `Unsolved_Futures.csv` | The futures that did not solve, with the solver's own status line |
| `Perturbed_Inputs.xlsx` | Every lever (Uncertainty_Table rows and Future_Changes rows): its draw in every future, where that sits in Min–Max, whether the data show it landed, the perturbed values and their ratio to the reference future |
| `Perturbed_Inputs_Dashboard.html` | The same as charts, plus the diagnosis and an "Inputs by set" explorer (Set → Parameter → Row, every year, every future). Opens offline if `plotly` is installed |
| `Inputs_by_Set.xlsx` | Every input row that differs between futures, every year, on one sheet per set: **Technology, Commodity (FUEL), Emission, UDC, Storage, Region** |
| `Inputs_by_Set_all_years.csv` | Every input value of every future, every year, including rows that do not differ |
| `Outputs_by_Set.xlsx` / `Outputs_by_Set_all_years.csv` | The same for the results of the solved futures |
| `Infeasibility_Diagnosis.xlsx` | For each unsolved future: verdict and leads, lever ranking, input rows outside the solved range, conflicting bounds, solver evidence |

Red rows are unsolved futures; grey rows are the reference future.

## Where the values come from

`RDM_Inputs.csv` holds only the variables ticked `Export = Y` on the
interface's Outputs sheet, and most of those are keyed by TECHNOLOGY. For each
future the tool reads the most complete source it can reach:

1. the future's own B2 input CSV,
   `execution\Executables\<future>_0\<future>_0_Input.csv`, which holds every
   parameter, including the FUEL-keyed `InputActivityRatio` /
   `OutputActivityRatio` and the EMISSION-keyed ones;
2. otherwise that future's rows of `RDM_Inputs.csv`.

So run it on the computer that solved the run for the full picture. On
another computer (only the run folder copied over) it falls back to
`RDM_Inputs.csv` and says so; set `Export = Y` for the parameters you need
and re-run `rdm collect` to carry them in the CSV.

The `Sources` sheet of `Inputs_by_Set.xlsx` records which file each future
was read from.

**UDC.** The OSTRAM model has no user-defined-constraint set today, so there
is no UDC sheet. If a model ever carries a `UDC` column, it is listed on its
own sheet without any change here.

## How the infeasibility diagnosis decides

Each unsolved future is compared with the futures that **did** solve, three
ways:

1. **Levers.** A draw beyond every solved draw, in the direction that
   tightens the model, makes the lever a *Suspect* (score 3). Tightening means
   higher for demand, minimum capacity / investment, activity lower limits,
   input-activity and emission ratios; lower for availability, maximum
   capacity / investment, activity upper limits, emission limits, operational
   life. Cost levers (CapitalCost, FixedCost, VariableCost) change the
   optimum, never feasibility, and are never blamed.
2. **Input rows of every set.** Every row that differs between futures is
   checked, year by year, against the range the solved futures span. This
   catches changes no lever names (knock-on effects of A3 rules, or B2
   adjustments).
3. **Bounds that cannot both hold** in that future: minimum above maximum new
   capacity, minimum above maximum total capacity, residual capacity above the
   total-capacity cap, activity lower above upper limit, a factor outside 0–1,
   a negative demand or ratio. A cap of −1 means "no cap" and is skipped. A
   conflict counts only if no solved future has it too.

The verdict is **Likely input cause** (a new conflict or a suspect lever),
**Possible input cause** (inputs outside the solved range in the tightening
direction), or **No perturbed input sets this future apart**: then the draws
are not the explanation, and the solver log is the place to look (time limit,
numerical trouble). A future that *stopped* or never ran is reported as such,
not as infeasible.

These are leads, not proofs. Confirm one by re-running the future with that
lever at a solved value. With fewer than three solved futures the comparison
is weak and the report says so.

## Requirements

pandas, numpy, openpyxl (all in the OSTRAM environment). `plotly` is
optional: with it the dashboard embeds plotly.js and works offline; without
it the page loads plotly.js from the jsDelivr CDN.

## Layout

```
diagnosis_for_rdm/
  run_diagnosis.py      entry point: CONFIG block + command line
  rdmdiag/
    run.py              find runs, read the run folder, solver status per future
    levers.py           tie each lever to the input rows it changes
    perturbed.py        Perturbed_Inputs.xlsx
    allsets.py          Inputs_by_Set / Outputs_by_Set (every set, every year)
    infeasibility.py    Infeasibility_Diagnosis.xlsx
    dashboard.py        Perturbed_Inputs_Dashboard.html
    excel.py            workbook formatting
  tests/test_rdmdiag.py builds a small run folder and checks every step
```
