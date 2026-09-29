"""Thermo blocks declare their enthalpy basis and entropy reference pressure.

TCKDB #520 refuses thermo that carries enthalpy content without
``enthalpy_reference_kind`` (and a declaration without content); TCKDB #529
never defaults ``reference_pressure_bar``. The adapter declares both from
ARC's conventions and checks every block with the shared
``enthalpy_reference_error`` rule before emitting it.
"""

import copy
import json
import os
from pathlib import Path
import shutil
from unittest import mock

import pytest
import yaml

from tckdb_arc import adapter as adapter_module
from tckdb_arc.adapter import TCKDBAdapter, _build_thermo_block
from tckdb_arc.config import TCKDBConfig
from tckdb_schemas.enthalpy_reference import enthalpy_reference_error
from tckdb_schemas.workflows.computed_reaction_upload import (
    BundleThermoIn,
    ComputedReactionUploadRequest,
)
from tckdb_schemas.workflows.computed_species_upload import (
    ComputedSpeciesUploadRequest,
    ThermoInBundle,
)

from test_adapter import (
    _StubClient,
    _StubResponse,
    _fake_output_doc,
    _full_record,
    _reaction_output_doc,
    _reaction_record,
)

FIXTURES = Path(__file__).parent / "fixtures"
TARGETS = (("ThermoInBundle", ThermoInBundle), ("BundleThermoIn", BundleThermoIn))
NASA = {
    "nasa_low": {"tmin_k": 100.0, "tmax_k": 1000.0,
                 "coeffs": [4.0, -1e-3, 2e-6, -1e-9, 4e-13, -29000.0, 1.0]},
    "nasa_high": {"tmin_k": 1000.0, "tmax_k": 5000.0,
                  "coeffs": [3.5, 1e-3, -2e-7, 1e-11, -3e-15, -28500.0, 5.0]},
}


def _build(record, target="ThermoInBundle", **kwargs):
    return _build_thermo_block(record, calc_keys_by_role={}, target_model=target, **kwargs)


def _thermo_blocks(obj):
    """Yield every mapping stored under a ``thermo`` key, at any depth."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "thermo" and isinstance(value, dict):
                yield value
            yield from _thermo_blocks(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _thermo_blocks(item)


@pytest.mark.parametrize("target,model", TARGETS)
@pytest.mark.parametrize("record", [
    {"h298_kj_mol": -235.1, "s298_j_mol_k": 282.6},
    dict(NASA),
    {"h298_kj_mol": -235.1, "s298_j_mol_k": 282.6, **NASA,
     "thermo_points": [{"temperature_k": 300.0, "cp_j_mol_k": 33.6, "h_kj_mol": -230.5,
                        "s_j_mol_k": 285.1, "g_kj_mol": -315.9}]},
], ids=["scalars", "nasa_only", "full"])
def test_enthalpy_and_entropy_content_is_declared(record, target, model):
    block = _build(record, target)
    assert block["enthalpy_reference_kind"] == "formation_298k"
    assert block["reference_pressure_bar"] == 1.01325
    assert enthalpy_reference_error(block) is None
    model.model_validate(block)


@pytest.mark.parametrize("target,model", TARGETS)
def test_cp_only_block_declares_nothing(target, model):
    block = _build({"thermo_points": [{"temperature_k": 300.0, "cp_j_mol_k": 33.6}]}, target)
    assert block["points"] == [{"temperature_k": 300.0, "cp_j_mol_k": 33.6}]
    assert "enthalpy_reference_kind" not in block
    assert "reference_pressure_bar" not in block
    assert enthalpy_reference_error(block) is None
    model.model_validate(block)


def test_point_gibbs_without_enthalpy_is_declared():
    # G = H - T*S sits on the record's enthalpy zero and the entropy's
    # standard state, so a G-only point needs both.
    block = _build({"thermo_points": [{"temperature_k": 300.0, "g_kj_mol": -315.9}]})
    assert block["enthalpy_reference_kind"] == "formation_298k"
    assert block["reference_pressure_bar"] == 1.01325
    assert enthalpy_reference_error(block) is None
    ThermoInBundle.model_validate(block)


def test_entropy_only_block_carries_pressure_but_no_declaration():
    block = _build({"s298_j_mol_k": 282.6,
                    "thermo_points": [{"temperature_k": 300.0, "s_j_mol_k": 285.1}]})
    assert "enthalpy_reference_kind" not in block
    assert block["reference_pressure_bar"] == 1.01325
    assert enthalpy_reference_error(block) is None


@pytest.mark.parametrize("recorded,expected", [
    (101325.0, 1.01325),   # current ARC: RMG's P0, recovered from its partition function
    (100000.0, 1.0),       # a recorded 1 bar is honored, never overwritten
    (None, 1.01325),       # older output.yml: RMG's hard-coded 1 atm
    ("junk", 1.01325),
    (-5.0, 1.01325),
    (True, 1.01325),       # a YAML boolean is not a pressure (float(True) / 1e5 = 1e-5 bar)
    (1.01325, 1.01325),    # written in bar, not Pa: 1e-5 bar is outside the window
    (float("nan"), 1.01325),
])
def test_reference_pressure_prefers_recorded_standard_state(recorded, expected):
    block = _build({"s298_j_mol_k": 282.6, "standard_state_pressure_pa": recorded})
    assert block["reference_pressure_bar"] == expected


@pytest.mark.parametrize("recorded", [True, 1.01325, 1e7, "101325"])
def test_implausible_recorded_pressure_warns_and_falls_back(recorded, caplog):
    block = _build({"s298_j_mol_k": 282.6, "standard_state_pressure_pa": recorded})
    assert block["reference_pressure_bar"] == 1.01325
    assert "malformed standard_state_pressure_pa" in caplog.text


def _adapter(tmp_path, *, upload=False, client=None, mode="all"):
    cfg = TCKDBConfig(
        enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=str(tmp_path),
        api_key_env="X_TCKDB_API_KEY", project_label="thermo", upload_mode=mode,
        upload=upload, preflight=False,
    )
    client = client or _StubClient(response=_StubResponse({"id": 1}))
    return TCKDBAdapter(cfg, project_directory=str(tmp_path),
                        client_factory=lambda c, k: client)


def _reaction_doc_with_thermo():
    doc = _reaction_output_doc()
    for sp in doc["species"]:
        sp["thermo"] = copy.deepcopy(_full_record()["thermo"])
    return doc


def _corpus_payloads(tmp_path):
    """Every payload the adapter builds over the golden, synthetic, and current-ARC fixtures."""
    payloads = []
    adapter = _adapter(tmp_path)

    def built(outcome):
        return json.loads(outcome.payload_path.read_text())

    golden = FIXTURES / "golden"
    phase3 = yaml.safe_load((golden / "phase3_output.yml").read_text())
    (tmp_path / "output").mkdir()
    shutil.copyfile(golden / "tckdb_evidence.json", tmp_path / "output" / "tckdb_evidence.json")
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        for record in phase3["species"]:
            if record.get("xyz") and record.get("opt_final_energy_hartree") is not None:
                payloads.append((ComputedSpeciesUploadRequest, built(
                    adapter.submit_computed_species_from_output(
                        output_doc=phase3, species_record=record))))
        for reaction in phase3["reactions"]:
            payloads.append((ComputedReactionUploadRequest, built(
                adapter.submit_computed_reaction_from_output(
                    output_doc=phase3, reaction_record=reaction))))
        payloads.append((None, built(adapter.submit_computed_ts_from_output(
            output_doc=phase3, ts_record=phase3["transition_states"][0],
            reaction_record=phase3["reactions"][0]))))
        payloads.append((ComputedSpeciesUploadRequest, built(
            adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(), species_record=_full_record()))))
        doc = _reaction_doc_with_thermo()
        payloads.append((ComputedReactionUploadRequest, built(
            adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=_reaction_record()))))

    # The current-ARC fixture is a parser-evidence corpus: its records carry
    # no geometry/levels (so no bundle builds) and no thermo. Pass whatever
    # thermo it does carry through the builder so a future fixture refresh
    # that adds thermo is covered automatically.
    current = yaml.safe_load((FIXTURES / "current_arc" / "output.yml").read_text())
    for record in [*current.get("species", []), *current.get("transition_states", [])]:
        for target, model in TARGETS:
            block = _build(record.get("thermo"), target)
            if block is not None:
                payloads.append((model, {"thermo": block}))
    return payloads


def test_shared_rule_accepts_every_thermo_block_in_the_corpus(tmp_path):
    payloads = _corpus_payloads(tmp_path)
    blocks = [block for _model, payload in payloads for block in _thermo_blocks(payload)]
    # Non-vacuous: golden H2 (species + both reaction slots), the synthetic
    # species, and all four synthetic reaction participants.
    assert len(blocks) == 8
    for block in blocks:
        assert enthalpy_reference_error(block) is None, block
        assert block["enthalpy_reference_kind"] == "formation_298k"
        assert block["reference_pressure_bar"] == 1.01325
    for model, payload in payloads:
        if model is ComputedSpeciesUploadRequest or model is ComputedReactionUploadRequest:
            model.model_validate(payload)
        elif model is not None:
            model.model_validate(payload["thermo"])


@pytest.fixture
def refused_declaration(monkeypatch):
    # A near-miss of the one legal value: the real shared rule refuses it
    # (enthalpy_reference_kind_unrecognized) while the schema still parses.
    monkeypatch.setattr(adapter_module, "_THERMO_ENTHALPY_REFERENCE_KIND", "Formation_298K")


def test_refused_species_thermo_is_omitted_and_surfaced(tmp_path, refused_declaration):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=_fake_output_doc(), species_record=_full_record())
    payload = json.loads(outcome.payload_path.read_text())
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert "thermo" not in payload
    ComputedSpeciesUploadRequest.model_validate(payload)
    expected = [{
        "code": "enthalpy_reference_kind_unrecognized",
        "message": enthalpy_reference_error({"enthalpy_reference_kind": "Formation_298K"})[1],
        "field": "thermo",
        "context": {"source": "tckdb_arc_self_check", "action": "thermo_omitted"},
    }]
    assert outcome.status == "skipped"
    assert outcome.warnings == expected
    assert sidecar["warnings"] == expected


def test_refused_reaction_thermo_is_omitted_and_never_sent(tmp_path, refused_declaration):
    server_warning = {"code": "server_finding", "message": "from the server"}
    client = _StubClient(response=_StubResponse({"id": 1, "warnings": [server_warning]}))
    doc = _reaction_doc_with_thermo()
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, upload=True, client=client).submit_computed_reaction_from_output(
            output_doc=doc, reaction_record=_reaction_record())
    posted = [call["json"] for call in client.calls if call["path"] != "/readyz"]
    assert len(posted) == 1
    assert list(_thermo_blocks(posted[0])) == []
    ComputedReactionUploadRequest.model_validate(posted[0])
    species_keys = [sp["key"] for sp in posted[0]["species"]]
    producer = [w for w in outcome.warnings if w.get("context", {}).get("source") == "tckdb_arc_self_check"]
    assert [w["field"] for w in producer] == [f"species[{key}].thermo" for key in species_keys]
    assert {w["code"] for w in producer} == {"enthalpy_reference_kind_unrecognized"}
    # Producer findings precede the server's, and both reach the sidecar.
    assert outcome.warnings == [*producer, server_warning]
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings


def test_refused_block_with_no_sink_is_still_omitted(refused_declaration, caplog):
    assert _build({"h298_kj_mol": -235.1}) is None
    assert "enthalpy_reference_kind_unrecognized" in caplog.text


def test_failed_upload_keeps_producer_warnings(tmp_path, refused_declaration):
    client = _StubClient(raise_exc=RuntimeError("server unreachable"))
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, upload=True, client=client).submit_computed_species_from_output(
            output_doc=_fake_output_doc(), species_record=_full_record())
    assert outcome.status == "failed"
    assert [call for call in client.calls if call["path"] != "/readyz"]
    expected = [{
        "code": "enthalpy_reference_kind_unrecognized",
        "message": enthalpy_reference_error({"enthalpy_reference_kind": "Formation_298K"})[1],
        "field": "thermo",
        "context": {"source": "tckdb_arc_self_check", "action": "thermo_omitted"},
    }]
    assert outcome.warnings == expected
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == expected


# CH4 without atom corrections: Arkane's H298 is the raw absolute energy
# (about -40.5 Hartree, i.e. -1.06e5 kJ/mol) plus the thermal increment.
_UNCORRECTED_SHIFT_KJ_MOL = -106_330.0
_R = 8.314462618


def _uncorrected(thermo):
    """Shift every enthalpy in an ARC thermo record by a raw electronic energy."""
    thermo = copy.deepcopy(thermo)
    if "h298_kj_mol" in thermo:
        thermo["h298_kj_mol"] += _UNCORRECTED_SHIFT_KJ_MOL
    for point in thermo.get("thermo_points", ()):
        for key in ("h_kj_mol", "g_kj_mol"):
            if key in point:
                point[key] += _UNCORRECTED_SHIFT_KJ_MOL
    for nasa_key in ("nasa_low", "nasa_high"):
        if nasa_key in thermo:
            thermo[nasa_key]["coeffs"][5] += _UNCORRECTED_SHIFT_KJ_MOL * 1000.0 / _R
    return thermo


def _magnitude_warning(field="thermo"):
    return {"code": "enthalpy_not_formation_magnitude", "field": field,
            "context": {"source": "tckdb_arc_self_check", "action": "thermo_omitted"}}


def test_uncorrected_species_thermo_is_omitted_and_surfaced(tmp_path):
    record = _full_record()
    record["thermo"] = _uncorrected(record["thermo"])
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=_fake_output_doc(), species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    assert "thermo" not in payload
    ComputedSpeciesUploadRequest.model_validate(payload)
    [warning] = outcome.warnings
    assert {k: warning[k] for k in ("code", "field", "context")} == _magnitude_warning()
    assert "atom-energy corrections" in warning["message"]
    assert "20000 kJ/mol" in warning["message"]
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings


@pytest.mark.parametrize("record", [
    {"h298_kj_mol": -235.1 + _UNCORRECTED_SHIFT_KJ_MOL, "s298_j_mol_k": 186.3},
    {"thermo_points": [{"temperature_k": 300.0, "cp_j_mol_k": 35.7,
                        "h_kj_mol": -74.8 + _UNCORRECTED_SHIFT_KJ_MOL}]},
    _uncorrected(NASA),
], ids=["h298", "point_h", "nasa_only"])
def test_uncorrected_enthalpy_is_refused_from_every_source(record):
    warnings = []
    assert _build(record, warnings=warnings) is None
    assert [{k: w[k] for k in ("code", "field", "context")} for w in warnings] == [
        _magnitude_warning()]


def test_formation_magnitude_enthalpy_passes():
    block = _build({"h298_kj_mol": -74.9, "s298_j_mol_k": 186.3,
                    "thermo_points": [{"temperature_k": 300.0, "h_kj_mol": -74.8}]})
    assert block["h298_kj_mol"] == -74.9
    assert block["enthalpy_reference_kind"] == "formation_298k"


@pytest.mark.parametrize("h298,refused", [
    (2.0e4, False), (-2.0e4, False), (2.0e4 + 1e-6, True), (-2.0e4 - 1e-6, True),
])
def test_formation_magnitude_bound_is_inclusive(h298, refused):
    for record in ({"h298_kj_mol": h298},
                   {"thermo_points": [{"temperature_k": 300.0, "h_kj_mol": h298}]}):
        warnings = []
        block = _build(record, warnings=warnings)
        assert (block is None) is refused
        assert [w["code"] for w in warnings] == (
            ["enthalpy_not_formation_magnitude"] if refused else [])
