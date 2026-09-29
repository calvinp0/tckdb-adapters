"""Preserve the actual rate law when translating RMG's reference temperature."""

import math

from _contract import contract_validate
import pytest
from tckdb_schemas.workflows.computed_reaction_upload import BundleKineticsIn

from tckdb_arc.adapter import _build_kinetics_block


def build(**overrides):
    record = {"A": 4.2e12, "A_units": "s^-1", "n": 1.5,
              "Ea": 40.0, "Ea_units": "kJ/mol", "dA": 1.2}
    record.update(overrides)
    result = _build_kinetics_block(
        kinetics_record=record, reactant_keys=["r"], product_keys=["p"],
        actor_calc_keys={}, ts_calc_keys={},
    )
    contract_validate(BundleKineticsIn, result)
    return result


@pytest.mark.parametrize("t0", [1.0, 298.15, 1000.0])
@pytest.mark.parametrize("n", [-2.0, 0.0, 1.5])
def test_reference_temperature_preserves_rates(t0, n):
    result = build(T0_k=t0, n=n)
    for temperature in (300.0, 750.0, 2000.0):
        boltzmann = math.exp(-40000 / (8.314462618 * temperature))
        producer_rate = 4.2e12 * (temperature / t0)**n * boltzmann
        deposited_rate = result["a"] * temperature**result["n"] * boltzmann
        assert deposited_rate == pytest.approx(producer_rate)
    # ARC's dA is multiplicative, so changing A's reference does not scale it.
    assert result["a_uncertainty"] == 1.2
    assert result["a_uncertainty_kind"] == "multiplicative"


def test_legacy_missing_reference_uses_one_kelvin():
    assert build()["a"] == 4.2e12


@pytest.mark.parametrize("t0", [None, 0, -1, "bad", float("inf"), float("nan")])
def test_invalid_reference_omits_prefactor_but_keeps_other_kinetics(t0, caplog):
    result = build(T0_k=t0)
    assert "a" not in result
    assert "a_units" not in result
    assert result["reported_ea"] == 40.0
    assert "cannot normalize A" in caplog.text


def test_nonunit_reference_requires_exponent():
    assert "a" not in build(T0_k=300, n=None)


@pytest.mark.parametrize("n", [-1000, 1000])
def test_unrepresentable_normalization_omits_prefactor(n):
    assert "a" not in build(T0_k=1000, n=n)
