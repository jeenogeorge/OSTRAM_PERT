# RDM futures

Robust Decision Making (RDM) asks how a plan performs across many futures. In
OSTRAM a future is a derived scenario: a root scenario plus an ordered list of
edits. The `rdm` command builds those derived scenarios from one Excel
workbook, runs them all through the normal pipeline, and collects one input
CSV and one output CSV for the whole set.

You work only in the workbook. Nothing under `config/` or `inputs/` changes:
each run lives in its own folder, `<workspace>/rdm/<Run_Name>/`.

First time? Follow [RDM step by step](rdm-pipeline.md): it goes from a fresh
clone to the results, and explains what happens inside at each step. This page
is the short reference.

## Quick start

```powershell
# 1. A workbook with dropdowns filled from the prepared root
python -m ostram --setup unescap_training rdm template --out Interface_OSTRAM_RDM.xlsx

# 2. Fill the Futures, Future_Changes and Uncertainty_Table sheets, then
python -m ostram --setup unescap_training rdm run --interface Interface_OSTRAM_RDM.xlsx
```

`--setup` and `--workspace` go before `rdm`, as for every command; without
them the South Asia setup is used. The root must be prepared once (any run of
it, `--compile-only` is enough) before `template` can fill the technology and
fuel dropdowns; the template works without them, with empty dropdowns.

| Command | What it does |
|---|---|
| `rdm template --out <file> [--root <root>] [--blank]` | Writes the interface workbook. `--blank` leaves out the worked example. |
| `rdm generate --interface <file> [--overwrite]` | Checks the workbook and writes the run folder. Runs nothing. Use it to review the futures before a long run. |
| `rdm run --interface <file> [--overwrite] [--compile-only] [--a-result-seed <dir>] [--env-name <env>]` | `generate`, then A3, B1 and B2 for every future, then `collect`. |
| `rdm collect --run <Run_Name or folder>` | Gathers the CSVs again from the solved futures, for example after a run stopped part way. |

`run` refuses an existing run folder unless `--overwrite` is given; change
`Run_Name` to keep earlier runs.

## How a future is built

1. Copy the root named in `Setup > Base_Scenario`.
2. Apply the shared VRE ceilings, when they apply (`Apply_VRE_Ceilings`; `Auto`
   uses them when the setup defines them, which South Asia does and the
   training setup does not).
3. Apply the future's rows of `Future_Changes`, top to bottom.
4. With `Samples_Per_Future` above 0, apply one sampled value of every
   uncertainty in `Uncertainty_Table` whose `Applies_To` includes the future.
5. Re-apply the base-year pin, as for every derived scenario.

Steps 1, 2, 3 and 5 are the existing [scenario edits](scenario-edits.md); the
`rdm` command only writes the edits for you.

### Layered sampling

With `Samples_Per_Future` = N, each named future `F01` that has at least one
uncertainty becomes `F01_s001` to `F01_sN`. With `Run_Central_Future` = Y,
`F01` itself, without sampled uncertainties, runs as well. A future that no
uncertainty applies to runs once, under its own name.

The values are drawn by Latin Hypercube sampling, uniform between `Min` and
`Max`, from `Random_Seed`. Sample `s` uses the same draw in every future, so
`F01_s007` and `F02_s007` differ only by the futures' own changes. Two rows
linked with `Linked_To` take the same position in their ranges.

The sampled uncertainties apply after the future's changes, so a `Multiply`
uncertainty scales the future's path, not the root's. For example, a future
that raises demand by 15 per cent and a demand uncertainty of 0.9 to 1.1 give
demand between 103.5 and 126.5 per cent of the root.

## The workbook

| Sheet | Content |
|---|---|
| `README` | How to fill the workbook. |
| `Setup` | `Run_Name`, `Base_Scenario`, `Samples_Per_Future`, `Random_Seed`, `Run_Central_Future`, `Apply_VRE_Ceilings`, `Model_First_Year`, `Model_Last_Year`. |
| `Futures` | `Future_ID`, `Name`, `Description`, `Run` (Y/N). A future with no changes is the root itself, a baseline. |
| `Future_Changes` | One row per change: `Future_ID`, `Sheet`, `Parameter`, `Selector_Type`, `Selector`, `Operation`, `Value`, `Year_From`, `Year_To`, `Note`. |
| `Uncertainty_Table` | One row per ranged uncertainty: `X_ID`, `Name`, `Applies_To` (`ALL` or Future_IDs), the same target columns, `Method`, `Min`, `Max`, `Year_From`, `Year_To`, `Linked_To`, `Note`. |
| `Outputs` | `Kind` (Input or Output), `Variable`, `Export` (Y/N): what reaches the RDM CSVs. |

A `Future_ID` or `X_ID` is a letter followed by letters or digits. A
`Future_ID` becomes the scenario name, so it must not be the name of a
registered scenario.

### Targets

| `Sheet` | Holds | `Selector_Type` |
|---|---|---|
| `Secondary Techs` | power plants, storage, interconnectors (`PWR`, `TRN`, `ELC`) | `Tech`, `Techs`, `Tech_Prefix` |
| `Primary Techs` | fuel supply (`MIN`) | the same |
| `Demand Techs` | grid tiers (`RNW`, `TRN`, `PWR`, `DSP`) | the same |
| `VariableCost` | fuel prices and other variable costs | the same |
| `Demand_Projection` | `SpecifiedAnnualDemand` per demand fuel | `Fuel`, `Fuels`, `Fuel_Prefix` |

`Techs` and `Fuels` take several names separated by commas. A `*_Prefix`
selects every name that starts with the text, for example `PWRSPV` for all
solar. The [data reference](data-reference.md#technology-prefixes) lists the
prefixes.

### Change operations

| Operation | `Value` | Years |
|---|---|---|
| `Multiply` | the factor | `Year_From`/`Year_To` limit it; blank means every year. |
| `Multiply_Ramp` | the final factor | Both required. The factor rises in a straight line from 1 at `Year_From` to `Value` at `Year_To`, then stays at `Value` up to `Model_Last_Year`. Years before `Year_From` are unchanged. |
| `Set` | the value | `Year_From`/`Year_To` limit it; blank means every year. |
| `Copy_From_Tech` | the source technology | Every year; not for demand. |

`Uncertainty_Table` takes `Multiply`, `Multiply_Ramp` and `Set`; the sampled
value replaces `Value`. Only one year given means from that year to
`Model_Last_Year`, or from `Model_First_Year` to that year.

### Checks

`generate` and `run` read the whole workbook first and list every problem,
each with its sheet and row, before anything runs. A change that names a
technology, fuel, parameter or year missing from the root stops the build of
that future in A3 with the message of the [scenario edits](scenario-edits.md#edits-that-match-nothing),
which names the future and suggests the closest name.

## The run folder

`<workspace>/rdm/<Run_Name>/` holds:

| File | Content |
|---|---|
| `interface.xlsx` | A copy of the workbook the run came from. |
| `scenarios/<future>/scenario.yaml` | Each future as a derived-scenario file: its root and its edits, with each edit's note pointing to its workbook row. |
| `futures.yaml` | The list of futures, read by the registry during the run. |
| `rdm_manifest.json` | The settings of the run and the exported variables. |
| `RDM_Experiment_Design.csv` | One row per future: `Future_ID`, `Named_Future`, `Future_Name`, `Sample` (0 for a central future) and the sampled value of every uncertainty. This is the table for scenario discovery. |
| `RDM_Inputs.csv` | The exported input parameters of every future: `Future_ID`, `Named_Future`, `Sample`, the set columns, then one column per parameter. |
| `RDM_Outputs.csv` | The exported result variables of every future, in the same layout. |
| `RDM_Run_Status.csv` | For each future: `solved`, `compiled` (inputs only, as after `--compile-only`) or `missing`, with the paths of its B2 files. |

`RDM_Inputs.csv` and `RDM_Outputs.csv` join to `RDM_Experiment_Design.csv`
on `Future_ID`. They are the record of the run: the per-future folders under
`<workspace>/execution/Executables/` and `<workspace>/preparation/A1_Outputs/`
are reused by the next run with the same Future_IDs. `rdm run` deletes each
future's `Executables/<future>_0` folder before it starts, so a future that
fails or is only compiled never shows an earlier run's results.

The [RDM step by step](rdm-pipeline.md) page walks through a run from a fresh clone,
explaining what happens inside at each step.

## How the futures reach the pipeline

`rdm run` sets the environment variable `OSTRAM_RDM_FUTURES` to the run's
`futures.yaml` and calls `python -m ostram run --skip-pull --scenarios
<futures>`. While it is set, the scenario registry adds each listed future as
a derived scenario of its root, with its `scenario.yaml` in the run folder, so
A3, B1 and B2 treat the futures exactly like the scenarios of
`config/scenarios/`. Outside an `rdm` run the variable is unset and the
registry is unchanged.

B2 solves the futures one after another unless `parallel: True` is set in the
execution configuration (see [configuration](configuration.md)).

## Limits

- A future's root is one of the setup's roots. A future cannot build on
  another derived scenario; copy that scenario's changes into the future's
  rows instead.
- A future has no `gross_import_cap` and no interconnector direction overlay.
- The edits change the first row of a technology and parameter on a sheet, so
  `CapacityFactor` (one row per timeslice on `Capacities`) is not a target.
- A future built on `C_Target_VRE` needs a solved `A_Calibrated_BAU`, passed
  with `--a-result-seed`, as for `C_Target_VRE` itself.
