"""Conformer mode carries statmech, applied energy corrections and rejected rotors (adapter 0.6.6, roadmap A13).

``ConformerUploadRequest`` (tckdb-schemas 0.54) accepts ``statmech`` (with
``torsions[]``, whose ``invalidated_reason`` is the only home for ARC's
``statmech.rejected_torsions``) and ``applied_energy_corrections[]``. The
adapter builds them with the computed-species route's own builders, so the rules
already on main (Hessian-gated treatments, BAC omission, ``atom_params``, the
Arkane release, the declared energy level) apply unchanged.

The conformer route accepts only ``freq`` and ``sp`` as additional
calculations (``_ALLOWED_ADDITIONAL_TYPES``), so no rotor scan is sent, and its
``StatmechTorsionIn`` refuses a torsion without its dihedral coordinates.
"""

import copy
import json
import os
from unittest import mock

import pytest

from _backend_level_rules import calculations_by_key, energy_level_verdict
from _contract import contract_validate
from tckdb_schemas.workflows.conformer_upload import ConformerUploadRequest

from test_adapter import _fake_output_doc, _full_record, _reaction_output_doc
from test_current_contract_mapping import _neutral_scan
from test_provenance_passthrough import ARKANE, FIXTURE, _benzene, _submit
from test_thermo_enthalpy_declaration import _adapter

TORSION = {"symmetry_number": 3, "treatment": "hindered_rotor", "dimension": 1,
           "atom_indices": [1, 2, 3, 4], "pivot_atoms": [2, 3], "barrier_kj_mol": 12.0,
           "source_scan_key": None}
REJECTED = {"rotor_index": 4, "invalidation_reason": "the barrier was too high. ",
            "atom_indices": [2, 3, 4, 5], "pivot_atoms": [3, 4], "dimension": 1,
            "source_log": "calcs/Species/x/scan_a1/output.out"}


def _conformer(tmp_path, doc, record):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="conformer").submit_from_output(
            output_doc=doc, species_record=record)
    return json.loads(outcome.payload_path.read_text()), outcome


def _benzene_with(tmp_path, *, torsions=(), rejected=(), evidence=True):
    doc, record = _benzene(tmp_path)
    if not evidence:
        (tmp_path / "output" / "parser_evidence.json").unlink()
    record["statmech"]["torsions"] = [dict(t) for t in torsions]
    record["statmech"]["rejected_torsions"] = [dict(t) for t in rejected]
    return doc, record


# ---------------------------------------------------------------- benzene


def test_benzene_conformer_payload_carries_statmech_and_corrections(tmp_path):
    payload, outcome = _submit(tmp_path, "conformer")
    contract_validate(ConformerUploadRequest, payload)
    assert payload["calculation"]["key"] == "opt"
    assert [c["key"] for c in payload["additional_calculations"]] == ["freq", "sp"]
    statmech = payload["statmech"]
    assert statmech["external_symmetry"] == 12
    assert statmech["statmech_treatment"] == "rrho"
    assert statmech["software_release"] == ARKANE
    assert statmech["workflow_tool_release"]["name"] == "ARC"
    assert statmech["freq_scale_factor"]["value"] == 0.999
    assert [(s["calculation_key"], s["role"]) for s in statmech["source_calculations"]] == [
        ("opt", "opt"), ("freq", "freq"), ("sp", "sp")]
    assert "torsions" not in statmech
    aecs = {c["scheme"]["kind"]: c for c in payload["applied_energy_corrections"]}
    assert set(aecs) == {"atom_energy", "bac_petersson"}
    assert all(c["source_calculation_key"] == "sp" for c in aecs.values())
    assert {"C", "H"} <= {p["element"] for p in aecs["atom_energy"]["scheme"]["atom_params"]}
    assert all(c["scheme"]["workflow_tool_release"]["name"] == "Arkane" for c in aecs.values())
    assert [c["key"] for c in aecs["bac_petersson"]["components"]] == ["C-C", "C-H", "C=C"]
    # The declared energy level is the sp's own (A11): spin_treatment included.
    calcs = calculations_by_key(payload["calculation"], payload["additional_calculations"])
    assert statmech["energy_level_of_theory"] == calcs["sp"]["level_of_theory"]
    assert energy_level_verdict(statmech, calcs) is None
    assert "thermo" not in payload  # the conformer upload has no thermo slot


def test_conformer_blocks_are_the_computed_species_blocks(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "c").mkdir()
    species, _ = _submit(tmp_path / "s", "computed_species")
    conformer, _ = _submit(tmp_path / "c", "conformer")
    assert conformer["applied_energy_corrections"] == species["applied_energy_corrections"]
    expected = dict(species["statmech"])
    got = dict(conformer["statmech"])
    # The bundle names ARC once at its root and its statmech inherits it; the
    # conformer request has no such slot, so its statmech says so itself.
    assert got.pop("workflow_tool_release") == species["workflow_tool_release"]
    assert got == expected


def test_incomplete_bac_is_omitted_as_on_the_species_route(tmp_path):
    doc, record = _benzene(tmp_path)
    bac = next(c for c in record["energy_corrections"] if c["correction_type"] == "bond_additivity")
    bac["components"] = None
    payload, outcome = _conformer(tmp_path, doc, record)
    assert [c["scheme"]["kind"] for c in payload["applied_energy_corrections"]] == ["atom_energy"]
    [warning] = [w for w in outcome.warnings
                 if w["code"] == "bac_correction_omitted_components_incomplete"]
    assert warning["context"]["species"] == record["label"]
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings


def test_no_corrections_and_no_statmech_leaves_the_payload_as_before(tmp_path):
    payload, _ = _conformer(tmp_path, _fake_output_doc(), _full_record())
    assert "statmech" not in payload and "applied_energy_corrections" not in payload
    # The calculations are keyed all the same (harmless, and stable).
    assert payload["calculation"]["key"] == "opt"


# ------------------------------------------------------- Hessian gating


def test_rotor_treatment_needs_hessian_evidence(tmp_path):
    doc, record = _benzene_with(tmp_path, torsions=[TORSION], evidence=False)
    payload, outcome = _conformer(tmp_path, doc, record)
    statmech = payload["statmech"]
    assert "statmech_treatment" not in statmech
    [torsion] = statmech["torsions"]
    assert "treatment_kind" not in torsion and torsion["symmetry_number"] == 3
    [warning] = [w for w in outcome.warnings if w["code"] == "statmech_treatment_not_stated"]
    assert warning["field"] == "statmech.statmech_treatment"


def test_rotor_treatment_with_hessian_evidence(tmp_path):
    doc, record = _benzene_with(tmp_path, torsions=[TORSION])
    payload, outcome = _conformer(tmp_path, doc, record)
    statmech = payload["statmech"]
    assert statmech["statmech_treatment"] == "rrho_1d"
    assert statmech["torsions"] == [{
        "torsion_index": 1, "treatment_kind": "hindered_rotor", "symmetry_number": 3,
        "dimension": 1,
        "coordinates": [{"coordinate_index": 1, "atom1_index": 1, "atom2_index": 2,
                         "atom3_index": 3, "atom4_index": 4}]}]
    assert "statmech_treatment_not_stated" not in [w["code"] for w in outcome.warnings]


# ------------------------------------------------- scans are not sendable


def test_no_scan_is_sent_and_the_torsion_drops_its_scan_link(tmp_path):
    doc, record = _benzene_with(tmp_path, torsions=[dict(TORSION, source_scan_key="scan_rotor_0")])
    record["rotor_scans"] = [_neutral_scan()]
    doc["scan_level"] = {"method": "b3lyp", "basis": "def2tzvp", "software": "gaussian"}
    payload, outcome = _conformer(tmp_path, doc, record)
    assert [c["type"] for c in payload["additional_calculations"]] == ["freq", "sp"]
    [torsion] = payload["statmech"]["torsions"]
    assert "source_scan_calculation_key" not in torsion
    [warning] = [w for w in outcome.warnings if w["code"] == "torsion_scan_not_built"]
    assert warning["context"]["scan_key"] == "scan_rotor_0"
    assert "only freq and sp" in warning["context"]["reason"]


# ------------------------------------------------------ rejected rotors


def test_rejected_rotors_become_invalidated_torsions_after_the_treated_ones(tmp_path):
    doc, record = _benzene_with(
        tmp_path, torsions=[TORSION, dict(TORSION, atom_indices=[3, 4, 5, 6])], rejected=[REJECTED])
    payload, _ = _conformer(tmp_path, doc, record)
    statmech = payload["statmech"]
    torsions = statmech["torsions"]
    assert [t["torsion_index"] for t in torsions] == [1, 2, 3]
    rejected = torsions[2]
    assert rejected == {
        "torsion_index": 3, "dimension": 1,
        "coordinates": [{"coordinate_index": 1, "atom1_index": 2, "atom2_index": 3,
                         "atom3_index": 4, "atom4_index": 5}],
        "invalidated_reason": "the barrier was too high. ",
        "note": "ARC rotor_index 4"}
    # ARC states neither for a rotor it rejected.
    assert "treatment_kind" not in rejected and "symmetry_number" not in rejected
    # The treatment is what the treated rotors give; a rejected one is not treated.
    assert statmech["statmech_treatment"] == "rrho_1d"
    assert all("invalidated_reason" not in t for t in torsions[:2])


def test_only_rejected_rotors_leave_plain_rrho(tmp_path):
    doc, record = _benzene_with(tmp_path, rejected=[REJECTED])
    payload, _ = _conformer(tmp_path, doc, record)
    assert payload["statmech"]["statmech_treatment"] == "rrho"
    [torsion] = payload["statmech"]["torsions"]
    assert torsion["torsion_index"] == 1 and torsion["invalidated_reason"]


@pytest.mark.parametrize("reason", ["", "  ", None])
def test_a_rotor_rejected_without_a_reason_says_so_and_invents_none(tmp_path, reason):
    doc, record = _benzene_with(tmp_path, rejected=[dict(REJECTED, invalidation_reason=reason)])
    payload, _ = _conformer(tmp_path, doc, record)
    [torsion] = payload["statmech"]["torsions"]
    assert torsion["invalidated_reason"] == "ARC rejected this rotor and recorded no reason"


def test_a_rejected_rotor_without_atoms_is_not_sent(tmp_path):
    bad = dict(REJECTED, rotor_index=7, atom_indices=None)
    doc, record = _benzene_with(tmp_path, rejected=[bad, REJECTED])
    payload, outcome = _conformer(tmp_path, doc, record)
    [torsion] = payload["statmech"]["torsions"]
    assert torsion["note"] == "ARC rotor_index 4"
    [warning] = [w for w in outcome.warnings if w["code"] == "rejected_torsion_not_sent"]
    assert warning["context"]["rotor_index"] == 7


def test_a_treated_rotor_without_atoms_is_not_sent_in_conformer_mode(tmp_path):
    # The bundle roots accept such a torsion; the conformer upload's refuses it.
    doc, record = _benzene_with(
        tmp_path, torsions=[dict(TORSION, atom_indices=None), dict(TORSION, atom_indices=[3, 4, 5, 6])])
    payload, outcome = _conformer(tmp_path, doc, record)
    assert [t["torsion_index"] for t in payload["statmech"]["torsions"]] == [2]
    [warning] = [w for w in outcome.warnings if w["code"] == "torsion_not_sent"]
    assert warning["context"]["torsion_position"] == 1


def test_the_computed_species_bundle_carries_rejected_rotors_since_0_61(tmp_path):
    """tckdb-schemas 0.61 gave ``StatmechTorsionInBundle`` ``invalidated_reason``; it has no ``note``."""
    doc, record = _benzene_with(tmp_path, torsions=[TORSION], rejected=[REJECTED])
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="computed_species").submit_computed_species_from_output(
            output_doc=doc, species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    treated, rejected = payload["statmech"]["torsions"]
    assert "invalidated_reason" not in treated
    assert rejected == {
        "torsion_index": 2, "dimension": 1,
        "coordinates": [{"coordinate_index": 1, "atom1_index": 2, "atom2_index": 3,
                         "atom3_index": 4, "atom4_index": 5}],
        "invalidated_reason": "the barrier was too high. "}
    # A rejected rotor was not treated: the treatment is what the treated rotors give.
    assert payload["statmech"]["statmech_treatment"] == "rrho_1d"


def test_the_computed_reaction_bundle_carries_rejected_rotors_since_0_61(tmp_path):
    doc = _reaction_output_doc()
    doc["species"][0]["statmech"] = {"external_symmetry": 1, "rejected_torsions": [dict(REJECTED)]}
    adapter = _adapter(tmp_path, mode="computed_reaction")
    payload = adapter._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0])
    (torsion,) = payload["species"][0]["statmech"]["torsions"]
    assert torsion["invalidated_reason"] == "the barrier was too high. " and "note" not in torsion


def test_bundle_route_reports_a_torsion_sent_without_coordinates(tmp_path):
    doc, record = _benzene_with(tmp_path, torsions=[dict(TORSION, atom_indices=None)])
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="computed_species").submit_computed_species_from_output(
            output_doc=doc, species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    assert "coordinates" not in payload["statmech"]["torsions"][0]
    [warning] = [w for w in outcome.warnings if w["code"] == "torsion_without_coordinates"]
    assert warning["context"]["torsion_index"] == 1
