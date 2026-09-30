"""ARC's wavefunction stability verdict reaches the opt's ``scf_stability`` (adapter 0.6.6, roadmap A10).

ARC runs its stability analysis once per species, from the optimization job: at
the opt level, on the geometry the opt converged to, with the opt's own
orbitals (``arc/scheduler.py::run_stability_job``), so its SCF reproduces the
wavefunction under test. ``wavefunction_stability`` therefore describes the
opt's wavefunction and the verdict goes on the primary opt calculation, never on
freq/sp (their own SCF may land on another solution), scans or IRCs. It is not
sent when ARC's record shows the opt sent is not the tested one
(``scf_reference.source == 'derived'``, ``measured_on_ts_guess``). The contract:
"Producers must only emit status = stable when an actual SCF / wavefunction
stability analysis was observed ... When unsure whether a stability analysis was
performed, omit the block ... Use status = inconclusive only when a stability
analysis was clearly attempted but its result could not be parsed."
"""

import copy
import json
import os
from unittest import mock

import pytest

from tckdb_arc.adapter import _scf_stability_payload

from test_adapter import _fake_output_doc, _full_record, _reaction_output_doc, _reaction_record
from test_provenance_passthrough import _benzene, _calculations, _submit
from test_thermo_enthalpy_declaration import _adapter, _reaction_doc_with_thermo


def _stability(verdict="stable", **extra):
    return {"verdict": verdict, "internal_instability": verdict == "internal_instability",
            "external_instability": verdict == "external_instability",
            "relaxations": [], "negative_eigenvectors": [], "lowest_eigenvalue": None,
            "restricted": True, "invalidates_analytic_freq": False,
            "log": "calcs/Species/x/stability/output.out", **extra}


def _record(stability=None, *, tested=True, freq="restricted", sp="restricted", **reference):
    record = _full_record()
    record["wavefunction_stability"] = stability
    record["scf_reference"] = {
        "source": None, "declared_number_of_radicals": None,
        "verdict": stability.get("verdict") if isinstance(stability, dict) else None,
        "verdict_restricted": tested, "measured_on_ts_guess": None,
        "freq_reference": freq, "sp_reference": sp, "reference_mismatch": freq != sp,
        "log": None, **reference}
    return record


def _species(tmp_path, record, doc=None):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=doc or _fake_output_doc(), species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    conformer = payload["conformers"][0]
    return {c["type"]: c for c in (conformer["primary_calculation"],
                                   *conformer["additional_calculations"])}


def _stability_by_type(calcs):
    return {kind: calc["scf_stability"] for kind, calc in calcs.items() if "scf_stability" in calc}


def test_a_stable_verdict_reaches_the_opt_only(tmp_path):
    calcs = _species(tmp_path, _record(_stability(lowest_eigenvalue=0.0123)))
    assert _stability_by_type(calcs) == {"opt": {"status": "stable", "lowest_eigenvalue": 0.0123}}


def test_never_on_freq_sp_even_when_their_reference_and_level_match(tmp_path):
    calcs = _species(tmp_path, _record(_stability("external_instability")))
    assert "scf_stability" not in calcs["freq"] and "scf_stability" not in calcs["sp"]


def test_coarse_opt_and_screened_conformers_do_not_carry_it(tmp_path):
    record = _record(_stability())
    record.update(coarse_opt_log="a.log", coarse_opt_output_xyz="C 0 0 0\nH 1 0 0",
                  coarse_opt_final_energy_hartree=-1.0, coarse_opt_n_steps=3)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=_fake_output_doc(), species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    conformer = payload["conformers"][0]
    with_block = [c["key"] for c in (conformer["primary_calculation"],
                                     *conformer["additional_calculations"])
                  if "scf_stability" in c]
    assert with_block == ["opt"]


def test_no_analysis_no_block(tmp_path):
    assert _stability_by_type(_species(tmp_path, _record(None))) == {}
    record = _record(_stability())
    del record["wavefunction_stability"]
    assert _stability_by_type(_species(tmp_path, record)) == {}


def test_benzene_without_an_analysis_sends_none(tmp_path):
    payload, _ = _submit(tmp_path, "computed_species")
    assert not any("scf_stability" in calc for calc in _calculations(payload))


@pytest.mark.parametrize("verdict,expected", [
    ("stable", {"status": "stable"}),
    ("internal_instability", {"status": "unstable", "instability_type": "internal"}),
    ("external_instability", {"status": "unstable", "instability_type": "external"}),
    ("unattributed_instability", {"status": "unstable"}),
    # An analysis ran whose verdict ARC could not read: never "stable".
    ("unknown", {"status": "inconclusive"}),
    ("something_new", None),
    (None, None),
])
def test_verdict_mapping(tmp_path, verdict, expected):
    stability = _stability(verdict)
    stability["verdict"] = verdict
    assert _species(tmp_path, _record(stability))["opt"].get("scf_stability") == expected


@pytest.mark.parametrize("source", ["derived"])
def test_a_derived_reference_means_the_opt_sent_is_not_the_tested_one(tmp_path, source):
    # ARC adopted the verdict and re-optimized at another reference.
    calcs = _species(tmp_path, _record(_stability("external_instability"), source=source))
    assert _stability_by_type(calcs) == {}


@pytest.mark.parametrize("source", [None, "declared"])
def test_other_sources_keep_the_verdict(tmp_path, source):
    calcs = _species(tmp_path, _record(_stability(), source=source))
    assert set(_stability_by_type(calcs)) == {"opt"}


def test_a_verdict_measured_on_an_abandoned_ts_guess_is_not_sent(tmp_path):
    calcs = _species(tmp_path, _record(_stability(), measured_on_ts_guess="TS0_guess1"))
    assert _stability_by_type(calcs) == {}


def test_a_stable_verdict_that_was_followed_to_a_stable_solution_is_not_sent(tmp_path):
    # ARC's ORCA reader sets followed_to_stable only when the tested analysis
    # opened unstable; a stable verdict with it is contradictory data.
    calcs = _species(tmp_path, _record(_stability("stable", followed_to_stable=True)))
    assert _stability_by_type(calcs) == {}


def test_a_followed_instability_stays_unstable_and_is_not_stabilized(tmp_path):
    calcs = _species(tmp_path, _record(_stability(
        "external_instability", followed_to_stable=True, n_analyses=2)))
    assert calcs["opt"]["scf_stability"] == {"status": "unstable", "instability_type": "external"}


@pytest.mark.parametrize("eigenvalue,kept", [(-0.02, True), (0, True), (True, False),
                                             (float("nan"), False), ("0.1", False)])
def test_lowest_eigenvalue_is_arcs_number_or_absent(tmp_path, eigenvalue, kept):
    calcs = _species(tmp_path, _record(_stability("internal_instability", lowest_eigenvalue=eigenvalue)))
    block = calcs["opt"]["scf_stability"]
    assert ("lowest_eigenvalue" in block) is kept
    if kept:
        assert block["lowest_eigenvalue"] == float(eigenvalue)


def test_the_console_summary_string_is_not_a_verdict(tmp_path):
    assert _stability_by_type(_species(tmp_path, _record("stable"))) == {}


def test_conformer_mode_puts_it_on_the_primary_opt(tmp_path):
    record = _record(_stability("internal_instability"))
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="conformer").submit_from_output(
            output_doc=_fake_output_doc(), species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    assert payload["calculation"]["scf_stability"] == {
        "status": "unstable", "instability_type": "internal"}
    assert not any("scf_stability" in c for c in payload["additional_calculations"])


def test_reaction_participants_carry_their_own_verdict(tmp_path):
    doc = _reaction_doc_with_thermo()
    doc["species"][0].update(_record(_stability())
                             | {k: doc["species"][0][k] for k in ("label", "smiles", "multiplicity", "xyz")})
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_reaction_from_output(
            output_doc=doc, reaction_record=_reaction_record())
    payload = json.loads(outcome.payload_path.read_text())
    with_block = [sp["key"] for sp in payload["species"]
                  if "scf_stability" in sp["conformers"][0]["calculation"]]
    assert with_block == [payload["species"][0]["key"]]
    assert not any("scf_stability" in c for sp in payload["species"] for c in sp["calculations"])


def test_helper_is_a_pure_function_of_arcs_record():
    assert _scf_stability_payload(_record(_stability())) == {"status": "stable"}
    assert _scf_stability_payload({"wavefunction_stability": _stability()}) is None
