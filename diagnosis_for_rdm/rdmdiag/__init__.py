"""Diagnosis tools for OSTRAM RDM runs.

These tools only read a finished (or partly finished) RDM run folder,
``<workspace>/setups/<setup>/rdm/<Run_Name>/``, and write what they find to
its ``diagnosis/`` subfolder. They are not part of the OSTRAM pipeline and
import nothing from the ``ostram`` package, so they can be run on their own,
at any time, on any run.
"""
