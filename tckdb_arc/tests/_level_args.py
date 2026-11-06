"""A corpus whose levels carry ARC-shaped ``args`` (the one shape no other fixture had).

ARC writes a level's ``args`` as ``{"keyword": {<job or "general">: "<ESS keywords>"}, "block": {...}}``
(``arc/level.py``, lower-cased; the output schema calls the key set "the user's, not ARC's").
Until TCKDB's typed method qualifiers (tckdb-schemas 0.158) the adapter flattened it into
``level_of_theory.keywords``; every other fixture's levels lacked ``args``, so no test saw that key.

``level_args_corpus`` is the Phase 3 golden document (species, reaction, TS, kinetics) with every level
carrying ``args``, plus an energy-correction record whose scheme level carries them too:

* the opt and freq levels are Gaussian, ``keyword.general`` ``int=dkh2`` (a relativistic qualifier) and
  ``keyword.opt`` ``scf=(maxcycle=200)`` (an SCF option, NOT a qualifier);
* the sp level is ORCA, ``keyword.general`` ``tightpno rijcosx`` (pno_truncation + ri_approximation)
  and a ``block`` entry (not a qualifier).
"""

import copy
import shutil
from pathlib import Path

import yaml

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = FIXTURES / "golden"

GAUSSIAN_ARGS = {
    "keyword": {"general": "int=dkh2", "opt": "scf=(maxcycle=200)"},
    "block": {},
}
ORCA_ARGS = {
    "keyword": {"general": "tightpno rijcosx"},
    "block": {"scf": "maxiter 200"},
}


def _scheme_level(software):
    args = ORCA_ARGS if software == "orca" else GAUSSIAN_ARGS
    return {"method": "dlpno-ccsd(t)" if software == "orca" else "wb97x-d",
            "basis": "def2-tzvp", "software": software, "args": copy.deepcopy(args)}


def level_args_corpus():
    """``(doc, sidecars)``: golden + kinetics with ``args`` on every level, as an ARC document."""
    doc = yaml.safe_load((GOLDEN / "phase3_output.yml").read_text())
    doc["project"] = "level_args"
    doc["arkane_git_commit"] = "7f4e9c2a1b3d5e6f708192a3b4c5d6e7f8091a2b"
    doc["opt_level"].update(args=copy.deepcopy(GAUSSIAN_ARGS))
    doc["freq_level"].update(args=copy.deepcopy(GAUSSIAN_ARGS))
    doc["sp_level"].update(software="orca", method="dlpno-ccsd(t)", args=copy.deepcopy(ORCA_ARGS))
    for record in [*doc["species"], *doc["transition_states"]]:
        versions = record.get("ess_versions")
        if versions and "sp" in versions:
            versions["sp"] = "ORCA 5.0.4"
    h2 = next(s for s in doc["species"] if s["label"] == "H2")
    h2["energy_corrections"] = [
        {"correction_type": "atom_energy", "model": "arkane_atom_energy",
         "level_of_theory": _scheme_level("orca"), "total": {"value": -0.0234, "unit": "hartree"},
         "components": [
             {"component_kind": "atom", "key": "H", "multiplicity": 2,
              "parameter_value": -0.4998, "parameter_unit": "hartree",
              "contribution_value": -0.0234}]},
    ]
    doc["reactions"][0]["kinetics"] = {
        "A": 2.5e13, "A_units": "cm^3/(mol*s)", "n": 1.5, "T0_k": 300.0,
        "Ea": 40.0, "Ea_units": "kJ/mol", "Tmin_k": 300.0, "Tmax_k": 2000.0,
        "dA": 1.3, "dn": 0.05, "dEa": 0.4, "dEa_units": "kJ/mol", "n_data_points": 30, "tunneling": "Eckart",
    }
    return doc, [GOLDEN / "tckdb_evidence.json"]


def materialize(root, doc, sidecars):
    """Write ``doc`` as ``<root>/output/output.yml`` beside its sidecars."""
    out = Path(root) / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "output.yml").write_text(yaml.safe_dump(doc, sort_keys=False))
    for sidecar in sidecars:
        shutil.copyfile(sidecar, out / Path(sidecar).name)
    return Path(root)


def iter_levels(obj):
    """Every mapping that looks like an ARC level (has ``method`` and ``software``), anywhere in ``obj``."""
    if isinstance(obj, dict):
        if "method" in obj and "software" in obj:
            yield obj
        for value in obj.values():
            yield from iter_levels(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from iter_levels(item)


def iter_keys(obj):
    """Every mapping key appearing anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from iter_keys(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from iter_keys(item)


def build_all_payloads(project, payload_dir):
    """Build (offline) every route's payload over ``project``; ``{route: payload}``."""
    import json
    import os
    from unittest import mock

    from tckdb_arc.adapter import TCKDBAdapter
    from tckdb_arc.config import TCKDBConfig
    from test_adapter import _StubClient, _StubResponse

    doc = yaml.safe_load((Path(project) / "output" / "output.yml").read_text())
    cfg = TCKDBConfig(enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=str(payload_dir),
                      api_key_env="X_TCKDB_API_KEY", project_label="level_args", upload_mode="all", upload=False)
    adapter = TCKDBAdapter(cfg, project_directory=str(project),
                           client_factory=lambda c, k: _StubClient(response=_StubResponse({"id": 1})))
    reaction = doc["reactions"][0]

    def payload(outcome):
        assert outcome.payload_path is not None
        return json.loads(Path(outcome.payload_path).read_text())

    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        return {
            "conformer": [payload(adapter.submit_from_output(
                output_doc=doc, species_record=sp)) for sp in doc["species"]],
            "computed_species": [payload(adapter.submit_computed_species_from_output(
                output_doc=doc, species_record=sp)) for sp in doc["species"]],
            "computed_reaction": payload(adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=reaction)),
            "transition_state": payload(adapter.submit_computed_ts_from_output(
                output_doc=doc, ts_record=doc["transition_states"][0], reaction_record=reaction)),
        }
