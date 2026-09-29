# Benzene, B3LYP/def2-TZVP (real ARC run)

`output.yml` (output schema 1.2) and `parser_evidence.json` copied verbatim
from a real ARC run (`~/code/arcbench/debug/benzene_b3lyp_def2tzvp/output/`,
2026-09-29; ARC 1.1.0 `c3e904ea`, Arkane 4.0.0 `e6f47b42`, Gaussian 16
Revision C.02). Only these two files were taken; logs and other outputs were
not. They contain run-relative paths (`calcs/Species/benzene/...`) and the run
timestamps, and no usernames, servers, job IDs or credentials.

It is the first real-ARC fixture with statmech and applied energy correction
records (Arkane atom energies and Petersson BAC). The run was uploaded to
production and drew `software_release_version_is_composite`,
`missing_software_release_provenance` (thermo and statmech) and
`missing_energy_correction_scheme_software` warnings, which adapter 0.6.1
resolves from data already in `output.yml` (`test_provenance_passthrough.py`).
