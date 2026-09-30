"""Keys ARC's output.yml does not write are not read (BRIDGE_ROADMAP A19).

Each mapping below used to read a key ARC never exports, which looked like
coverage but could only ever act on a hand-edited or third-party record. The
species/kinetics/reaction cases live beside their neighbours in test_adapter.py;
these are the calculation-level ones.
"""

from tckdb_arc.adapter import (
    _final_settings_for_calc,
    _sp_result_payload,
    _spin_diagnostic_payload,
    _build_thermo_block,
    _correction_records_from_record,
    _is_output_schema_1_0,
)


def test_sp_energy_is_read_only_from_arcs_sp_energy_hartree():
    assert _sp_result_payload({"label": "x", "sp_energy_hartree": -1.5}) == {
        "electronic_energy_hartree": -1.5}
    # ``electronic_energy_hartree`` is TCKDB's name, not an ARC key.
    assert _sp_result_payload({"label": "x", "electronic_energy_hartree": -1.5}) is None


def test_irc_has_no_final_settings_source():
    # ARC writes opt/coarse-opt/freq/sp ``*_final_settings`` only.
    record = {"irc_final_settings": {"stepsize": 10}, "opt_final_settings": {"a": 1}}
    assert _final_settings_for_calc(species_record=record, calc_role="irc") is None
    assert _final_settings_for_calc(species_record=record, calc_role="opt") == {"a": 1}


def test_spin_diagnostic_has_no_note():
    block = {"s_squared": 0.76, "s_squared_expected": 0.75, "log": "calcs/sp.log",
             "note": "not an ARC key"}
    assert _spin_diagnostic_payload({"sp_spin_diagnostic": block}) == {
        "s_squared": 0.76, "s_squared_expected": 0.75}


def test_thermo_cp_data_is_read_only_for_output_schema_1_0():
    # 1.0 wrote Cp points as ``cp_data``; 1.1 renamed it ``thermo_points``.
    thermo = {"s298_j_mol_k": 130.0, "tmin_k": 300.0, "tmax_k": 1000.0,
              "cp_data": [{"temperature_k": 300.0, "cp_j_mol_k": 29.0}]}
    modern = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle")
    assert "points" not in (modern or {})
    legacy = _build_thermo_block(
        thermo, calc_keys_by_role={}, target_model="ThermoInBundle", legacy_cp_data=True)
    assert [p["temperature_k"] for p in legacy["points"]] == [300.0]
    thermo["thermo_points"] = [{"temperature_k": 400.0, "cp_j_mol_k": 30.0}]
    modern = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle")
    assert [p["temperature_k"] for p in modern["points"]] == [400.0]


def _aec_record():
    return {"energy_corrections": [{
        "correction_type": "atom_energy", "model": "arkane_atom_energy",
        "level_of_theory": {"method": "b3lyp", "basis": "6-31g", "software": "gaussian"},
        "total": {"value": -0.5, "unit": "hartree"}, "components": [],
        "parameter_table": {"values": {"H": -0.499, "C": -37.8}},
    }]}


def test_atom_energy_parameter_table_is_read_only_for_output_schema_1_0():
    (modern,) = _correction_records_from_record(_aec_record())
    assert "atom_params" not in modern["scheme"]
    (legacy,) = _correction_records_from_record(_aec_record(), legacy_1_0=True)
    assert legacy["scheme"]["atom_params"] == [
        {"element": "C", "value": -37.8}, {"element": "H", "value": -0.499}]
    assert legacy["scheme"]["units"] == "hartree"


def test_schema_version_alone_selects_the_legacy_reads():
    assert _is_output_schema_1_0({"schema_version": "1.0"})
    assert not _is_output_schema_1_0({"schema_version": "1.1"})
    assert not _is_output_schema_1_0({"schema_version": "1.2"})
    assert not _is_output_schema_1_0({})


def test_standalone_ts_reaction_sends_reversible_true_whatever_the_record_says():
    # ARC writes no ``reversible`` key; the standalone TS route requires the
    # field, so it sends True (maintainer decision, TCKDB issue #583).
    import copy

    from test_adapter import _reaction_output_doc
    from test_ts_upload import _adapter

    doc = copy.deepcopy(_reaction_output_doc())
    doc["transition_states"][0]["converged"] = True
    doc["reactions"][0]["reversible"] = False
    payload = _adapter()._compose_transition_state_request(
        output_doc=doc, ts_record=doc["transition_states"][0],
        reaction_record=doc["reactions"][0])
    assert payload["reaction"]["reversible"] is True


def test_schema_1_0_document_keeps_its_cp_points_end_to_end(tmp_path):
    import copy

    from test_adapter import _fake_output_doc, _full_record
    from tckdb_arc.adapter import TCKDBAdapter
    from tckdb_arc.config import TCKDBConfig

    adapter = TCKDBAdapter(TCKDBConfig(enabled=True, upload=False, base_url="http://x",
                                       payload_dir=str(tmp_path)))
    record = copy.deepcopy(_full_record())
    record["thermo"] = {"s298_j_mol_k": 130.0, "tmin_k": 300.0, "tmax_k": 1000.0,
                        "cp_data": [{"temperature_k": 300.0, "cp_j_mol_k": 29.0}]}
    for version, expected in (("1.0", [300.0]), ("1.1", [])):
        doc = _fake_output_doc()
        doc["schema_version"] = version
        payload = adapter._build_computed_species_payload(
            output_doc=doc, species_record=record, conformer_key="conf0")
        assert [p["temperature_k"] for p in payload.get("thermo", {}).get("points", [])] == expected
