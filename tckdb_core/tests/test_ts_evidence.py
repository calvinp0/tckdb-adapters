"""The transition-state validation-evidence shapes and the offline replica of TCKDB's rules for them."""

import pytest

from tckdb_core import ts_evidence as ev
from tckdb_core.ts_evidence_rules import KIND_FIELDS, offline_evidence_errors


def _energies(ts=-1.0, reactants=(-0.6, -0.5), products=(-1.2,)):
    out = [ev.energy_entry("ts", ts, "ts_sp")]
    out += [ev.energy_entry(f"reactant:{i}", e, f"r{i}_sp") for i, e in enumerate(reactants, 1)]
    out += [ev.energy_entry(f"product:{i}", e, f"p{i}_sp") for i, e in enumerate(products, 1)]
    return out


def test_irc_record_shape_and_field_order():
    record = ev.irc_record(True, source_calculation_key="irc", rationale="why",
                           participant_mappings=({"reactant:1": [1]}, {"product:1": [1]}))
    assert list(record) == ["kind", "passed", "rationale", "source_calculation_key",
                            "reactant_participant_mapping", "product_participant_mapping"]
    assert list(ev.irc_record(False, source_calculation_key="irc", rationale="r")) == [
        "kind", "passed", "rationale", "source_calculation_key"]


def test_imaginary_mode_record_shape_and_field_order():
    record = ev.imaginary_mode_record(
        True, source_calculation_key="freq", imaginary_frequency_count=1, frequency_cm1=1200.0,
        mode_displacement_agrees=True, rationale="r")
    assert list(record) == ["kind", "passed", "source_calculation_key", "imaginary_frequency_count",
                            "imaginary_frequency_cm1", "mode_displacement_agrees", "rationale"]
    assert record["imaginary_frequency_cm1"] == -1200.0
    bare = ev.imaginary_mode_record(
        False, source_calculation_key="freq", imaginary_frequency_count=None, frequency_cm1=None,
        mode_displacement_agrees=None, rationale="r")
    assert list(bare) == ["kind", "passed", "source_calculation_key", "rationale"]


def test_freq_result_facts():
    result = {"n_imag": 2, "reaction_coordinate_mode_index": 2, "imag_freq_cm1": -10.0,
              "modes": [{"mode_index": 1, "frequency_cm1": -90.0}, {"mode_index": 2, "frequency_cm1": -1300.0}]}
    assert ev.freq_result_imaginary_facts(result) == (2, 2, -1300.0)
    assert ev.freq_result_imaginary_facts({"n_imag": 1, "imag_freq_cm1": -900.0}) == (1, None, -900.0)
    assert ev.freq_result_imaginary_facts({"n_imag": True}) == (None, None, None)
    assert ev.freq_result_imaginary_facts({"n_imag": 1, "imag_freq_cm1": float("nan")}) == (1, None, None)


def test_energy_ordering_contradiction():
    margin = 1.0
    assert ev.energy_ordering_contradiction(True, _energies(ts=-1.0), margin_kj_mol=margin) is None
    reason, code = ev.energy_ordering_contradiction(True, _energies(ts=-1.2), margin_kj_mol=margin)
    assert "do not put the saddle point more than 1 kJ/mol above the reactant side" in reason and code is None
    assert ev.energy_ordering_contradiction(False, _energies(ts=-1.2), margin_kj_mol=margin) is None
    reason, code = ev.energy_ordering_contradiction(False, _energies(ts=-1.0), margin_kj_mol=margin)
    assert "computed on stale energies" in reason and code == "verdict_contradicted_by_stated_energies"


def test_finite_float():
    assert ev.finite_float("1.5") == 1.5
    assert ev.finite_float(True) is None and ev.finite_float(None) is None and ev.finite_float("x") is None
    assert ev.finite_float(float("inf")) is None


def _bundle(record):
    calc = lambda key, type_: {"key": key, "type": type_}  # noqa: E731
    return {
        "reactant_keys": ["r1", "r2"], "product_keys": ["p1"],
        "species": [{"key": "r1", "conformers": [{"calculation": calc("r1_sp", "sp")}]},
                    {"key": "r2", "conformers": [{"calculation": calc("r2_sp", "sp")}]},
                    {"key": "p1", "conformers": [{"calculation": calc("p1_sp", "sp")}]}],
        "transition_state": {"calculation": calc("ts_sp", "sp"),
                             "calculations": [calc("ts_freq", "freq"), calc("ts_irc", "irc")],
                             "validation_evidence": [record]},
    }


def test_replica_accepts_what_the_builders_make():
    record = ev.energy_ordering_record(True, "r", _energies())
    record["energies"] = [dict(e, source_calculation_key=k) for e, k in zip(
        record["energies"], ["ts_sp", "r1_sp", "r2_sp", "p1_sp"])]
    assert offline_evidence_errors(_bundle(record), standalone=False) == []
    irc = ev.irc_record(True, source_calculation_key="ts_irc", rationale="r")
    assert offline_evidence_errors(_bundle(irc), standalone=False) == []
    assert set(irc) <= KIND_FIELDS["irc"]


def test_replica_refuses_what_the_rules_refuse():
    record = ev.energy_ordering_record(True, "r", _energies(ts=-1.2))
    errors = offline_evidence_errors(_bundle(record), standalone=False)
    assert any("puts the saddle point at or below the reactant side" in e for e in errors)
    assert any("energy_ordering is refused on the standalone" in e
               for e in offline_evidence_errors({"validation_evidence": [record], "primary_opt": {"type": "opt", "key": "o"}},
                                                standalone=True))
    imaginary = ev.imaginary_mode_record(
        True, source_calculation_key="ts_irc", imaginary_frequency_count=1, frequency_cm1=900.0,
        mode_displacement_agrees=None, rationale="r")
    assert any("must name a freq calculation" in e for e in offline_evidence_errors(_bundle(imaginary), standalone=False))
