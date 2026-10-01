"""Regressions for ARC facts accepted by the current TCKDB upload contract."""

import copy

from _contract import contract_validate
import pytest

import test_adapter as fixtures
from tckdb_arc.adapter import TCKDBAdapter, _build_applied_energy_corrections
from tckdb_arc.config import TCKDBConfig
from tckdb_schemas.energy_correction import EnergyCorrectionSchemeRef
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.transition_state_upload import TransitionStateUploadRequest


@pytest.fixture
def adapter(tmp_path):
    return TCKDBAdapter(TCKDBConfig(
        enabled=True, upload=False, base_url="http://localhost", payload_dir=str(tmp_path),
    ))


def _neutral_scan():
    return {
        "key": "scan_rotor_0",
        "result": {
            "dimension": 1, "relaxed": True,
            "coordinate": {
                "coordinate_type": "dihedral", "atom_indices": [1, 2, 3, 4],
                "index_base": 1, "unit": "degree", "requested_step_size": 120.0,
            },
            "samples": [
                {"source_index": 0, "angle_degrees": 0.0, "relative_energy_kj_mol": 0.0},
                {"source_index": 1, "angle_degrees": 120.0, "relative_energy_kj_mol": 2.0},
            ],
        },
        "constraints": [{"coordinate_type": "distance", "atom_indices": [0, 1],
                         "index_base": 0, "target_value": 1.4}],
    }


def _scan_doc():
    doc = fixtures._reaction_output_doc()
    ts = doc["transition_states"][0]
    ts["xyz"] += "\nH 0 0 1"
    ts["rotor_scans"] = [_neutral_scan()]
    return doc


def test_ts_scan_survives_reaction_bundle_with_distinct_namespace(adapter):
    doc = _scan_doc()
    doc["species"][0]["rotor_scans"] = [_neutral_scan()]
    payload = adapter._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0],
    )
    ts_scan = next(c for c in payload["transition_state"]["calculations"] if c["type"] == "scan")
    assert ts_scan["key"] == "ts_scan_rotor_0"
    assert ts_scan["depends_on"] == [{"parent_calculation_key": "ts_opt", "role": "scan_parent"}]
    assert ts_scan["constraints"][0]["atom1_index"] == 1
    assert ts_scan["scan_result"]["points"][1]["relative_energy_kj_mol"] == 2.0
    species_keys = {c["key"] for c in payload["species"][0]["calculations"]}
    assert "r0_scan_rotor_0" in species_keys
    assert ts_scan["key"] not in species_keys
    contract_validate(ComputedReactionUploadRequest, payload)


def test_standalone_ts_carries_its_rotor_scan_with_the_points(adapter, caplog):
    """tckdb-schemas 0.64: the standalone request accepts ``scan`` calculations and their ``scan_result``."""
    doc = _scan_doc()
    payload = adapter._compose_transition_state_request(
        output_doc=doc, ts_record=doc["transition_states"][0],
        reaction_record=doc["reactions"][0],
    )
    (scan,) = [c for c in payload["additional_calculations"] if c["type"] == "scan"]
    assert scan["scan_result"]["points"][1]["relative_energy_kj_mol"] == 2.0
    assert scan["constraints"][0]["atom1_index"] == 1
    # the bundle-only keys are stripped, as for every standalone calculation
    assert not {"key", "depends_on", "geometry_key", "artifacts"} & set(scan)
    assert "use computed_reaction mode to retain them" not in caplog.text
    contract_validate(TransitionStateUploadRequest, payload)


def test_reaction_thermo_and_coarse_opt_link_to_own_participant(adapter):
    doc = fixtures._reaction_output_doc()
    sp = doc["species"][0]
    sp.update({"thermo": {"h298_kj_mol": -100.0}, "coarse_opt_log": "coarse.log",
               "coarse_opt_output_xyz": "C 0 0 0\nH 1.1 0 0"})
    payload = adapter._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0],
    )
    participant = payload["species"][0]
    assert participant["thermo"]["source_calculations"] == [
        {"calculation_key": f"r0_{role}", "role": role} for role in ("opt", "freq", "sp")
    ]
    coarse = next(c for c in participant["calculations"] if c["key"] == "r0_opt_coarse")
    assert coarse["conformer_key"] == participant["conformers"][0]["key"]
    assert "geometry_key" not in coarse
    assert "1.1" in coarse["output_geometries"][0]["geometry"]["xyz_text"]
    contract_validate(ComputedReactionUploadRequest, payload)


@pytest.mark.parametrize("version", [None, "legacy-2024"])
def test_legacy_correction_scheme_validates_without_removed_version(version):
    record = fixtures._aec_record()
    record["scheme"]["version"] = version
    original = copy.deepcopy(record)
    result = _build_applied_energy_corrections([record])[0]
    assert "version" not in result["scheme"]
    if version:
        assert version in result["scheme"]["note"]
    contract_validate(EnergyCorrectionSchemeRef, result["scheme"])
    assert record == original


def _calculation(record, *, kind="sp", level=None):
    return TCKDBAdapter._calculation_payload(
        fixtures._fake_output_doc(), record, calc_type=kind,
        level=level or {"method": "b3lyp", "basis": "def2-svp", "software": "gaussian"},
        ess_job_key=kind,
    )


def test_actual_ess_and_version_stay_paired():
    calc = _calculation({"ess_software": {"opt": "gaussian", "sp": "orca"},
                         "ess_versions": {"opt": "16", "sp": "6.0"}})
    assert calc["software_release"] == {"name": "orca", "version": "6.0"}


def test_missing_version_does_not_borrow_another_ess_banner():
    calc = _calculation({"ess_software": {"opt": "gaussian", "sp": "orca"},
                         "ess_versions": {"opt": "16"}})
    assert calc["software_release"] == {"name": "orca"}


def test_observed_ess_can_supply_missing_requested_software():
    calc = _calculation({"ess_software": {"sp": "orca"}}, level={"method": "b3lyp"})
    assert calc["software_release"] == {"name": "orca"}


def test_measured_spin_reference_is_job_specific():
    record = {"scf_reference": {"sp_reference": "unrestricted", "freq_reference": "restricted"}}
    assert _calculation(record)["level_of_theory"]["spin_treatment"] == "unrestricted"
    assert _calculation(record, kind="freq")["level_of_theory"]["spin_treatment"] == "restricted"
    assert "spin_treatment" not in _calculation(record, kind="opt")["level_of_theory"]


def test_freq_hessian_method_is_a_typed_parameter_in_both_roots(adapter):
    doc = fixtures._reaction_output_doc()
    doc["transition_states"][0]["freq_hessian_method"] = "finite_difference_gradient"
    payload = adapter._build_computed_reaction_payload(output_doc=doc, reaction_record=doc["reactions"][0])
    freq = next(c for c in payload["transition_state"]["calculations"] if c["type"] == "freq")
    assert freq["parameters"][0]["canonical_key"] == "freq.hessian_method"
    assert freq["parameters"][0]["canonical_value"] == "finite_difference_gradient"
    contract_validate(ComputedReactionUploadRequest, payload)
    standalone = adapter._compose_transition_state_request(
        output_doc=doc, ts_record=doc["transition_states"][0], reaction_record=doc["reactions"][0],
    )
    contract_validate(TransitionStateUploadRequest, standalone)


def test_unknown_hessian_method_is_not_invented():
    assert "parameters" not in _calculation({"freq_hessian_method": "unknown"}, kind="freq")


@pytest.mark.parametrize("mode", ["species", "reaction", "ts"])
def test_scan_without_provenance_is_omitted_and_torsion_summary_survives(adapter, caplog, mode):
    doc = _scan_doc()
    doc.pop("scan_level", None)
    record = doc["species"][0]
    record["rotor_scans"] = [_neutral_scan()]
    record["statmech"] = {"torsions": [{
        "symmetry_number": 3, "treatment": "hindered_rotor",
        "source_scan_key": "scan_rotor_0",
    }]}
    if mode == "species":
        payload = adapter._build_computed_species_payload(
            output_doc=doc, species_record=record, conformer_key="conf0",
        )
        calculations = payload["conformers"][0]["additional_calculations"]
        statmech = payload["statmech"]
    else:
        payload = adapter._build_computed_reaction_payload(
            output_doc=doc, reaction_record=doc["reactions"][0],
        )
        owner = payload["transition_state"] if mode == "ts" else payload["species"][0]
        calculations = owner["calculations"]
        statmech = payload["species"][0]["statmech"]
    assert not any(calc["type"] == "scan" for calc in calculations)
    torsion = statmech["torsions"][0]
    assert torsion["symmetry_number"] == 3
    assert "source_scan_calculation_key" not in torsion
    assert "no level of theory available for scan" in caplog.text


@pytest.mark.parametrize("mode", ["species", "reaction", "ts"])
def test_scan_uses_distinct_explicit_level_and_software(adapter, mode):
    doc = _scan_doc()
    doc["scan_level"] = {"method": "b3lyp", "basis": "def2-svp", "software": "orca"}
    record = doc["species"][0]
    record["rotor_scans"] = [_neutral_scan()]
    for rec in [record, doc["transition_states"][0]]:
        rec["ess_software"] = {"opt": "gaussian"}
        rec["ess_versions"] = {"opt": "g16"}
    if mode == "species":
        payload = adapter._build_computed_species_payload(
            output_doc=doc, species_record=record, conformer_key="conf0",
        )
        calculations = payload["conformers"][0]["additional_calculations"]
    else:
        payload = adapter._build_computed_reaction_payload(
            output_doc=doc, reaction_record=doc["reactions"][0],
        )
        owner = payload["transition_state"] if mode == "ts" else payload["species"][0]
        calculations = owner["calculations"]
    scan = next(calc for calc in calculations if calc["type"] == "scan")
    assert scan["level_of_theory"]["method"] == "b3lyp"
    assert scan["level_of_theory"]["basis"] == "def2-svp"
    assert scan["software_release"] == {"name": "orca"}


def _build_with_torsion_scan(adapter, mode, scan_level):
    """Build ``mode``'s payload for a record whose torsion names its exported scan.

    Returns ``(payload, warnings, scan calcs of the scan's owner, species statmech)``;
    in ``ts`` mode the owner is the transition state, which also exports the
    scan (a TS has no torsion slot, so only the scan calc itself is checked).
    """
    doc = _scan_doc()
    doc["scan_level"] = scan_level
    record = doc["species"][0]
    record["rotor_scans"] = [_neutral_scan()]
    record["statmech"] = {"torsions": [{
        "symmetry_number": 3, "treatment": "hindered_rotor",
        "source_scan_key": "scan_rotor_0",
    }]}
    for rec in [record, doc["transition_states"][0]]:
        rec["ess_software"] = {"opt": "gaussian"}  # ARC never records ess_software['scan']
    warnings = []
    if mode == "species":
        payload = adapter._build_computed_species_payload(
            output_doc=doc, species_record=record, conformer_key="conf0", warnings=warnings,
        )
        calculations = payload["conformers"][0]["additional_calculations"]
        statmech = payload["statmech"]
    else:
        payload = adapter._build_computed_reaction_payload(
            output_doc=doc, reaction_record=doc["reactions"][0], warnings=warnings,
        )
        owner = payload["transition_state"] if mode == "ts" else payload["species"][0]
        calculations = owner["calculations"]
        statmech = payload["species"][0]["statmech"]
        if mode == "ts":
            # The standalone TS request is built too; the conftest hook
            # validates it (it carries no scans or torsions).
            adapter._compose_transition_state_request(
                output_doc=doc, ts_record=doc["transition_states"][0],
                reaction_record=doc["reactions"][0],
            )
    return payload, warnings, [c for c in calculations if c["type"] == "scan"], statmech


@pytest.mark.parametrize("mode", ["species", "reaction", "ts"])
def test_scan_level_without_software_drops_the_torsion_link_with_a_warning(adapter, mode):
    # Reachable from real ARC: Level.as_dict() carries no software for e.g.
    # b2plyp-d4/def2tzvp, and ARC never records ess_software['scan'], so the
    # scan calc cannot be built. The torsion must not keep naming it (TCKDB
    # would refuse the whole upload); the conftest hook validates the payload.
    payload, warnings, scans, statmech = _build_with_torsion_scan(
        adapter, mode, {"method": "b2plyp-d4", "basis": "def2tzvp", "method_type": "dft"})
    assert scans == []
    torsion = statmech["torsions"][0]
    assert torsion["symmetry_number"] == 3
    assert "source_scan_calculation_key" not in torsion
    dropped = [w for w in warnings if w["code"] == "torsion_scan_not_built"]
    # The species' torsion lost its link, whichever route carried it.
    assert len(dropped) == 1
    [warning] = dropped
    assert warning["field"] == ("statmech" if mode == "species" else "species[r0_CHO].statmech")
    assert warning["context"]["action"] == "torsion_scan_link_omitted"
    assert warning["context"]["scan_key"] == "scan_rotor_0"
    assert "missing software" in warning["context"]["reason"]


@pytest.mark.parametrize("mode", ["species", "reaction", "ts"])
def test_scan_level_with_software_builds_and_links_the_scan(adapter, mode):
    payload, warnings, scans, statmech = _build_with_torsion_scan(
        adapter, mode, {"method": "b2plyp-d4", "basis": "def2tzvp", "method_type": "dft",
                        "software": "gaussian"})
    [scan] = scans
    assert scan["software_release"]["name"] == "gaussian"
    expected = {"species": "scan_rotor_0", "reaction": "r0_scan_rotor_0",
                "ts": "ts_scan_rotor_0"}[mode]
    assert scan["key"] == expected
    linked = statmech["torsions"][0]["source_scan_calculation_key"]
    assert linked == ("scan_rotor_0" if mode == "species" else "r0_scan_rotor_0")
    assert not [w for w in warnings if w["code"] == "torsion_scan_not_built"]


@pytest.mark.payload_refused_by_contract
def test_missing_scan_provenance_does_not_hide_unknown_torsion_reference(adapter):
    doc = _scan_doc()
    doc.pop("scan_level", None)
    record = doc["species"][0]
    record["rotor_scans"] = [_neutral_scan()]
    record["statmech"] = {"torsions": [{
        "symmetry_number": 3, "treatment": "hindered_rotor",
        "source_scan_key": "nonexistent_scan",
    }]}
    payload = adapter._build_computed_species_payload(
        output_doc=doc, species_record=record, conformer_key="conf0",
    )
    assert payload["statmech"]["torsions"][0]["source_scan_calculation_key"] == "nonexistent_scan"
