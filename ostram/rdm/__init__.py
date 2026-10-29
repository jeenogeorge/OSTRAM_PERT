"""Robust Decision Making (RDM) futures for OSTRAM.

A user describes a set of named futures, and ranged uncertainties layered
inside them, in one Excel interface.  ``python -m ostram rdm`` turns the
interface into derived scenarios (one per future), runs them through the
normal pipeline, and collects one RDM input CSV and one RDM output CSV.

Modules:

* :mod:`ostram.rdm.interface`: writes the template and reads a filled workbook.
* :mod:`ostram.rdm.design`: expands futures and samples, and writes the run folder.
* :mod:`ostram.rdm.collect`: gathers the solved futures into the RDM CSVs.
* :mod:`ostram.rdm.cli`: the ``rdm`` command.
"""
