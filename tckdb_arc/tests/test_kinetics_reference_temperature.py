"""RMG's reference temperature T0 travels as TCKDB's ``t0_k`` (tckdb-schemas 0.63), with A unnormalised.

Contract (``BundleKineticsIn.t0_k``): "Reference temperature T0 of the Arrhenius expression, in K,
meaning k = A * (T / T0)**n * exp(-Ea / (R * T)). Defaults to 1 K ... It must satisfy
0 < t0_k <= 10000. It applies to the scalar a, n and reported_ea of a modified-Arrhenius record."
The 0.63 entry adds: "The server stores ``a`` as sent (it is A at T0, not A rescaled)". Before 0.63
the adapter sent a = A / T0**n and lost the T0 the rate was fitted with; it now sends ARC's A and T0.
"""

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


def evaluate(block, temperature):
    """k(T) the way a consumer of the stored record evaluates it (the 0.63 contract)."""
    t0 = block.get("t0_k", 1.0)
    return (block["a"] * (temperature / t0) ** block["n"]
            * math.exp(-block["reported_ea"] * 1000 / (8.314462618 * temperature)))


@pytest.mark.parametrize("t0", [1.0, 298.15, 1000.0, 10000.0])
@pytest.mark.parametrize("n", [-2.0, 0.0, 1.5])
def test_reference_temperature_preserves_rates(t0, n):
    result = build(T0_k=t0, n=n)
    # A is ARC's own, not A / T0**n: the T0 is stated instead.
    assert result["a"] == 4.2e12
    assert result["n"] == n
    assert result.get("t0_k") == (None if t0 == 1.0 else t0)
    for temperature in (300.0, 750.0, 2000.0):
        producer_rate = 4.2e12 * (temperature / t0) ** n * math.exp(
            -40000 / (8.314462618 * temperature))
        assert evaluate(result, temperature) == pytest.approx(producer_rate)
    # ARC's dA is multiplicative, so changing A's reference does not scale it.
    assert result["a_uncertainty"] == 1.2
    assert result["a_uncertainty_kind"] == "multiplicative"


def test_legacy_missing_reference_uses_one_kelvin_and_sends_no_t0():
    block = build()
    assert block["a"] == 4.2e12 and "t0_k" not in block


def test_a_stated_one_kelvin_is_the_contract_default_and_is_not_repeated():
    assert "t0_k" not in build(T0_k=1.0)


@pytest.mark.parametrize("t0", [None, 0, -1, "bad", float("inf"), float("nan"), True, 10000.5, 1e6])
def test_invalid_reference_omits_prefactor_but_keeps_other_kinetics(t0, caplog):
    result = build(T0_k=t0)
    assert "a" not in result
    assert "a_units" not in result
    assert "t0_k" not in result
    assert result["reported_ea"] == 40.0
    assert "cannot state T0_k" in caplog.text


def test_nonunit_reference_does_not_need_the_exponent():
    """A at T0 is stated as it is; the old normalisation needed n, the contract's reading does not."""
    block = build(T0_k=300, n=None)
    assert block["a"] == 4.2e12 and block["t0_k"] == 300.0 and "n" not in block


@pytest.mark.parametrize("n", [-1000, 1000])
def test_extreme_exponents_no_longer_make_the_prefactor_unrepresentable(n):
    block = build(T0_k=1000, n=n)
    assert block["a"] == 4.2e12 and block["t0_k"] == 1000.0


def test_t0_is_stated_only_beside_a():
    """With no A there is no A at T0 to qualify: no ``t0_k`` is sent."""
    block = build(A=None, T0_k=300.0)
    assert "a" not in block and "t0_k" not in block


def test_the_contract_bounds_are_the_ones_the_adapter_enforces():
    from tckdb_schemas import contract
    schema = contract.json_schema("ComputedReactionUploadRequest")["$defs"]["BundleKineticsIn"]["properties"]["t0_k"]
    assert (schema["exclusiveMinimum"], schema["maximum"]) == (0, 10000.0)
    from tckdb_arc.adapter import _MAX_ARRHENIUS_T0_K
    assert _MAX_ARRHENIUS_T0_K == schema["maximum"]
