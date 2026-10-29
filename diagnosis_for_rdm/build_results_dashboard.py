# -*- coding: utf-8 -*-
"""
build_results_dashboard.py — only the RDM results dashboard, for one run.

Writes <run>/diagnosis/RDM_Outputs_Dashboard.html from the run's
RDM_Outputs.csv and opens it. It is the "results_dashboard" step of
run_diagnosis.py on its own.

Spyder: set RUN below (e.g. "test01") and press F5. None = ask.
Prompt:  python diagnosis_for_rdm/build_results_dashboard.py --run test01
"""

import sys

RUN = None          # e.g. "test01"
SETUP = None        # only if two setups have a run of that name
WORKSPACE = None    # None = OSTRAM_WORKSPACE, else <this clone>/workspace

if __name__ == "__main__":
    import run_diagnosis

    argv = sys.argv[1:] or [a for a in (
        *(["--run", RUN] if RUN else []),
        *(["--setup", SETUP] if SETUP else []),
        *(["--workspace", WORKSPACE] if WORKSPACE else []),
    )]
    sys.exit(run_diagnosis.main(argv + ["--steps", "results_dashboard"]))
