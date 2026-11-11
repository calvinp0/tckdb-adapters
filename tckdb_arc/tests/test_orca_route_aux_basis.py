"""An ORCA keyword line is never compared with the level: its auxiliary basis is not a ``method/basis``.

ARC exports an ORCA route without the leading ``!`` and its auxiliary-basis sets carry a slash
(``aug-cc-pvtz/c``, ``def2/J``). ``route_level.route_method_basis`` reads the one ``a/b`` token of a
Gaussian line, so an ORCA DLPNO-CCSD(T)-F12 single point read as method ``aug-cc-pvtz``, basis ``c`` and
contradicted its own (correct) level: the sp calculation was dropped, or, for an atom's primary sp, the
whole reaction failed. The route is compared only when the record states the job ran in Gaussian.

The second half pins how a contradiction is handled in the reaction route: a skipped sp calculation
warns and the reaction builds, while a participant's primary calculation (the one it cannot do without)
refuses the reaction with a message that names the participant.
"""

import copy

import pytest

from test_arc_schema_1_3_levels import (
    _adapter, _calcs, _codes, _doc13, _reaction_payload, _record, _species_payload, _ts_calcs)
from tckdb_arc import adapter as adapter_module
from tckdb_core import route_level

ORCA_ROUTE = "uhf dlpno-ccsd(t)-f12 cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightpno"
ORCA_SP = {"method": "dlpno-ccsd(t)-f12", "basis": "cc-pvtz-f12", "auxiliary_basis": "aug-cc-pvtz/c",
           "cabs": "cc-pvtz-f12-cabs", "software": "orca"}


def _orca_doc():
    """``_doc13`` with every record's sp an ORCA DLPNO-CCSD(T)-F12 job, as ARC's output.yml states it."""
    doc = _doc13()
    for record in (*doc["species"], *doc["transition_states"]):
        record["levels"]["sp"] = dict(ORCA_SP)
        record["sp_route"] = ORCA_ROUTE
        record["ess_software"]["sp"] = "orca"
        record["ess_versions"]["sp"] = "ORCA 6.0.0"
    return doc


def _contradicted(doc, adapter):
    noted = doc.get(adapter_module._CONTRADICTIONS_KEY)
    return list(noted or [])


def test_the_orca_route_alone_does_read_as_a_contradiction_to_the_bare_parser():
    """The premise: the parser, given the ORCA line, reports the false contradiction."""
    assert route_level.route_contradicts_level(ORCA_ROUTE, ORCA_SP) == \
        "route aug-cc-pvtz/c vs level dlpno-ccsd(t)-f12/cc-pvtz-f12"


# ---- the ORCA sp builds on every route -------------------------------------------------------

def test_an_orca_sp_is_built_on_the_species_route(tmp_path):
    doc = _orca_doc()
    label = doc["species"][0]["label"]
    warnings = []
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, label, warnings))
    assert "sp" in calcs and calcs["sp"]["software_release"]["name"] == "orca"
    assert calcs["sp"]["level_of_theory"]["aux_basis"] == "aug-cc-pvtz/c"
    assert calcs["sp"]["level_of_theory"]["cabs_basis"] == "cc-pvtz-f12-cabs"
    assert "level_contradicted_by_route" not in _codes(warnings)


def test_an_orca_sp_is_built_on_the_conformer_route(tmp_path):
    adapter, doc = _adapter(tmp_path), _orca_doc()
    marked = adapter._with_adaptive_levels(doc, [])
    warnings = []
    payload = adapter._build_payload(
        output_doc=marked, species_record=_record(marked, doc["species"][0]["label"]), warnings=warnings)
    sp = next(c for c in payload["additional_calculations"] if c["type"] == "sp")
    assert sp["software_release"]["name"] == "orca"
    assert _contradicted(marked, adapter) == [] and "level_contradicted_by_route" not in _codes(warnings)


def test_an_orca_sp_is_built_on_the_reaction_route_for_participants_and_the_ts(tmp_path):
    adapter, doc = _adapter(tmp_path), _orca_doc()
    marked = adapter._with_adaptive_levels(doc, [])
    warnings = []
    payload = adapter._build_computed_reaction_payload(
        output_doc=marked, reaction_record=marked["reactions"][0], warnings=warnings)
    assert _contradicted(marked, adapter) == [] and "level_contradicted_by_route" not in _codes(warnings)
    assert _ts_calcs(payload)["ts_sp"]["software_release"]["name"] == "orca"
    for species in payload["species"]:
        keys = {c["key"] for c in [species["conformers"][0]["calculation"],
                                    *species["conformers"][0].get("additional_calculations", []),
                                    *species.get("calculations", [])]}
        assert any(k.endswith("_sp") for k in keys), keys


# ---- Gaussian is still compared --------------------------------------------------------------

def test_a_gaussian_route_naming_another_basis_still_contradicts_the_level(tmp_path):
    doc = _doc13()
    record = doc["species"][0]
    record["opt_route"] = "#P opt wb97xd/def2svp"          # the level says def2tzvp
    with pytest.raises(ValueError, match="level_contradicted_by_route"):
        _species_payload(_adapter(tmp_path), doc, record["label"])


def test_a_gaussian_sp_route_naming_another_basis_is_skipped_with_the_contradiction_noted(tmp_path):
    doc = _doc13()
    record = doc["species"][0]
    record["levels"]["sp"] = {"method": "wb97xd", "basis": "def2tzvp"}
    record["sp_route"] = "#P wb97xd/def2svp"
    record["ess_software"]["sp"] = "gaussian"
    record["ess_versions"]["sp"] = "Gaussian 16, Revision A.03"
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    calcs = _calcs(adapter._build_computed_species_payload(
        output_doc=marked, species_record=_record(marked, record["label"]), conformer_key="c0", warnings=[]))
    assert "sp" not in calcs and "opt" in calcs
    assert [(label, kind) for label, kind, _ in _contradicted(marked, adapter)] == [(record["label"], "sp")]


def test_a_second_slash_token_on_a_gaussian_line_is_not_a_contradiction():
    level = {"method": "b3lyp", "basis": "def2tzvp"}
    assert route_level.route_contradicts_level("#P b3lyp/def2tzvp/auto", level) is None
    assert route_level.route_contradicts_level("#P b3lyp/def2tzvp def2tzvp/auto", level) is None
    assert route_level.route_contradicts_level("#P b3lyp/def2svp", level) is not None


# ---- reaction route: skip-and-warn versus refuse ---------------------------------------------

def test_a_participants_contradicted_sp_is_skipped_and_the_reaction_still_builds(tmp_path):
    doc = _doc13()
    for record in (doc["species"][1], doc["transition_states"][0]):
        record["levels"]["sp"] = {"method": "wb97xd", "basis": "def2tzvp"}
        record["sp_route"] = "#P wb97xd/def2svp"
    record["ess_software"]["sp"] = "gaussian"
    record["ess_versions"]["sp"] = "Gaussian 16, Revision A.03"
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    warnings = []
    payload = adapter._build_computed_reaction_payload(
        output_doc=marked, reaction_record=marked["reactions"][0], warnings=warnings)
    adapter._report_adaptive_omissions(marked, warnings)
    assert "ts_sp" not in _ts_calcs(payload)
    assert "level_contradicted_by_route" in _codes(warnings)


def _atom_participant_doc():
    doc = _orca_doc()
    atom = doc["species"][1]                      # CH4 stands in for an H atom (the reaction's atom map is not built here)
    atom.update(xyz="H 0.0 0.0 0.0", smiles="[H]", formula="H", multiplicity=2, xyz_isotopes=[1])
    atom["levels"].update(opt=None, freq=None)
    atom.update(opt_log=atom["sp_log"], freq_log=None, opt_route=None, freq_route=None,
                sp_energy_hartree=-0.5)
    atom["ess_software"].update(opt=None, freq=None)
    return doc, atom


def test_an_orca_atom_participant_builds_its_primary_sp_in_a_reaction(tmp_path):
    doc, atom = _atom_participant_doc()
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    block, keys = adapter._build_reaction_species_block(
        output_doc=marked, species_record=_record(marked, atom["label"]), actor_key="r1_CH4",
        calc_prefix="r1")
    assert block["conformers"][0]["calculation"]["type"] == "sp" and keys == {"sp": "r1_sp"}


def test_a_contradicted_primary_calculation_refuses_the_reaction_naming_the_participant(tmp_path):
    """The primary calculation cannot be skipped; the error says which participant and why."""
    doc, atom = _atom_participant_doc()
    atom["ess_software"]["sp"] = "gaussian"          # a genuine Gaussian atom job whose route names another basis
    atom["levels"]["sp"] = {"method": "wb97xd", "basis": "def2tzvp"}
    atom["sp_route"] = "#P wb97xd/def2svp"
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    with pytest.raises(ValueError) as caught:
        adapter._build_computed_reaction_payload(
            output_doc=marked, reaction_record=marked["reactions"][0], warnings=[])
    text = str(caught.value)
    assert "level_contradicted_by_route" in text
    assert "'CH4'" in text and "primary calculation" in text and "reaction is not uploaded" in text
    assert _contradicted(marked, adapter)


# ---- the gate is an allow-list on a stated Gaussian program ------------------------------------

def test_an_irc_route_is_compared_only_when_the_irc_logs_are_stated_gaussian():
    ts = _doc13()["transition_states"][0]
    level = {"method": "wb97xd", "basis": "def2tzvp"}
    ts["irc_log_routes"] = ["#P irc=(forward) wb97xd/def2svp"] * 2           # genuinely another basis
    assert adapter_module._route_contradiction(ts, "irc", "irc", level) is not None   # gaussian: compared
    ts["ess_software"]["irc"] = "orca"
    assert adapter_module._route_contradiction(ts, "irc", "irc", level) is None
    del ts["ess_software"]["irc"]
    assert adapter_module._route_contradiction(ts, "irc", "irc", level) is None


def test_a_route_with_no_stated_program_for_its_job_is_not_compared():
    record = _doc13()["species"][0]
    level = {"method": "wb97xd", "basis": "def2tzvp"}
    record["opt_route"] = "#P opt wb97xd/def2svp"
    assert adapter_module._route_contradiction(record, "opt", "opt", level) is not None
    del record["ess_software"]["opt"]
    assert adapter_module._route_contradiction(record, "opt", "opt", level) is None
    record["ess_software"]["opt"] = "molpro"
    assert adapter_module._route_contradiction(record, "opt", "opt", level) is None


def test_a_contradicted_ts_primary_refuses_the_reaction_naming_the_ts(tmp_path):
    doc = _doc13()
    ts = doc["transition_states"][0]
    ts["opt_route"] = "#P opt wb97xd/def2svp"          # the level says def2tzvp, Gaussian
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    with pytest.raises(ValueError) as caught:
        adapter._build_computed_reaction_payload(
            output_doc=marked, reaction_record=marked["reactions"][0], warnings=[])
    text = str(caught.value)
    assert f"transition state {ts['label']!r}: its primary calculation cannot be built" in text
    assert "level_contradicted_by_route" in text and caught.value.__cause__ is not None
