"""Rewrite wrapped calculation results into the computed-reaction route's flat shape.

The computed-species bundle (``CalculationInBundle``) wraps results in nested
``opt_result`` / ``freq_result`` / ``sp_result`` dicts; the computed-reaction route's
``CalculationIn`` carries the same data as flat fields. Any producer that builds the
species-shaped calculation and posts it on the reaction route needs the same
translation, so it lives here, with the completeness guard that refuses to let a
field silently vanish in it.

The error messages of the guard name ``adapter.py`` and the tables by their old
underscore-prefixed names; they are kept byte-for-byte as they were before the move.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Result-shape adapter: ``CalculationInBundle`` (computed-species)
# wraps results in nested ``opt_result``/``freq_result``/``sp_result``
# dicts; the network_pdep ``CalculationIn`` (which the computed-reaction
# endpoint extends) carries the same data as flat fields. The mapping
# below is the full v0 translation; the producer's calculation builder produces
# the wrapped shape, and :func:`flatten_result_fields` rewrites it to
# the flat shape in place when building reaction bundles.
REACTION_FLAT_RESULT_FIELDS: dict[str, dict[str, str]] = {
    "opt_result": {
        "converged": "opt_converged",
        "n_steps": "opt_n_steps",
        "final_energy_hartree": "opt_final_energy_hartree",
    },
    "freq_result": {
        "n_imag": "freq_n_imag",
        "imag_freq_cm1": "freq_imag_freq_cm1",
        "zpe_hartree": "freq_zpe_hartree",
        "reaction_coordinate_mode_index": "freq_reaction_coordinate_mode_index",
    },
    "sp_result": {
        "electronic_energy_hartree": "sp_electronic_energy_hartree",
    },
}

# Wrapped-result top-level fields with no 1:1 entry in
# ``REACTION_FLAT_RESULT_FIELDS`` above but that ARE carried onto the
# reaction route by dedicated code further down in
# :func:`flatten_result_fields`, rather than the generic src->dst copy
# loop. ``freq_result.modes`` is the only one today: it fans out into
# both ``freq_frequencies_cm1`` and ``freq_imaginary_dispositions``.
REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE: dict[str, frozenset[str]] = {
    "freq_result": frozenset({"modes"}),
}

# Wrapped-result top-level fields that are deliberately NOT carried onto
# the reaction route's flat shape, keyed by wrapped_key -> {field:
# reason}. Empty today: every top-level field ``OptResultPayload`` /
# ``FreqResultPayload`` / ``SPResultPayload`` can carry already has a
# mapping above or a dedicated handler. Kept as a real (checked) table
# rather than omitted so a future field that genuinely can't be carried
# has an explicit place to land, with a reason, instead of the
# completeness guard below being silenced by deleting its check.
REACTION_RESULT_FIELDS_NOT_CARRIED: dict[str, dict[str, str]] = {}

# Per-mode (``FrequencyModePayload``) fields consumed while building
# ``freq_frequencies_cm1`` / ``freq_imaginary_dispositions`` above.
# ``mode_index`` and ``is_imaginary`` are read as indexing/control data
# and not re-emitted under their own name (``mode_index`` becomes the
# mode's position in ``freq_frequencies_cm1`` and its key in
# ``freq_imaginary_dispositions``; ``is_imaginary`` is implied on the
# flat side by the sign of ``frequency_cm1`` — see
# ``shared/calculation_in.py::freq_result_of``).
REACTION_MODE_FIELDS_HANDLED = frozenset({
    "mode_index", "frequency_cm1", "is_imaginary", "imaginary_disposition",
})

# ``FrequencyModePayload`` fields with no home at all on the reaction
# route: ``CalculationIn.freq_frequencies_cm1`` is a bare ``list[float]``
# and ``freq_imaginary_dispositions`` is a bare ``{mode_index:
# disposition}`` map (``shared/calculation_in.py``) — neither has a slot
# for per-mode metadata beyond frequency and imaginary disposition. This
# is a structural limit of the flat reaction shape itself, not an
# adapter oversight: ``shared/calculation_in.py::freq_result_of``
# reconstructs ``FrequencyModePayload`` from the flat fields using only
# ``mode_index``/``frequency_cm1``/``is_imaginary``/
# ``imaginary_disposition``, so nothing downstream could receive these
# even if this adapter tried to carry them. ``_freq_result_payload``
# does not currently populate any of them either, so nothing is lost in
# practice today; they are listed here (with the schema-level reason)
# so the completeness guard has a real, checked table to point at
# instead of an unexamined "everything else is fine".
REACTION_MODE_FIELDS_NOT_CARRIED: dict[str, str] = {
    "reduced_mass_amu": "no flat CalculationIn slot for per-mode reduced mass",
    "force_constant_mdyne_angstrom": "no flat CalculationIn slot for per-mode force constant",
    "ir_intensity_km_mol": "no flat CalculationIn slot for per-mode IR intensity",
    "raman_activity": "no flat CalculationIn slot for per-mode Raman activity",
    "symmetry_label": "no flat CalculationIn slot for per-mode symmetry label",
    "note": "no flat CalculationIn slot for per-mode free-text notes",
}


def flatten_result_fields(calc: dict[str, Any]) -> None:
    """Convert wrapped ``opt_result``/``freq_result``/``sp_result`` into flat fields.

    Mutates ``calc`` in place: each wrapped result dict is removed and
    its values are promoted to the network_pdep-style flat field names.
    A no-op for IRC and any other calc type without a wrapped result —
    those carry their data through ``parameters_json`` instead.

    Completeness guard: any key inside a wrapped result (or inside one
    of ``freq_result``'s ``modes`` entries) that is not accounted for by
    ``REACTION_FLAT_RESULT_FIELDS``, ``REACTION_RESULT_FIELDS_
    HANDLED_ELSEWHERE``/``REACTION_MODE_FIELDS_HANDLED``, or the
    corresponding "not carried" table raises ``ValueError`` instead of
    being silently dropped. Before this guard existed, any such key
    (e.g. a new field a species-shape builder starts emitting without
    updating this module) would vanish here with no error and no test
    failure — the reaction upload would simply lack data the species
    upload has, discovered only by a human diffing two uploads of the
    "same" calculation.
    """
    for wrapped_key, field_map in REACTION_FLAT_RESULT_FIELDS.items():
        result = calc.pop(wrapped_key, None)
        if not isinstance(result, Mapping):
            continue
        handled_elsewhere = REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE.get(
            wrapped_key, frozenset()
        )
        not_carried = REACTION_RESULT_FIELDS_NOT_CARRIED.get(wrapped_key, {})
        unknown = set(result) - set(field_map) - handled_elsewhere - set(not_carried)
        if unknown:
            raise ValueError(
                f"{wrapped_key} carries field(s) {sorted(unknown)!r} with no "
                f"reaction-route flat-field mapping, explicit handler, or "
                f"documented 'not carried' justification. Update "
                f"_REACTION_FLAT_RESULT_FIELDS, "
                f"_REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE, or "
                f"_REACTION_RESULT_FIELDS_NOT_CARRIED in adapter.py before "
                f"this field can silently vanish on the computed-reaction "
                f"route."
            )
        for src, dst in field_map.items():
            if src in result:
                calc[dst] = result[src]
        if wrapped_key == "freq_result":
            modes = result.get("modes")
            if modes:
                for m in modes:
                    mode_unknown = (
                        set(m)
                        - REACTION_MODE_FIELDS_HANDLED
                        - set(REACTION_MODE_FIELDS_NOT_CARRIED)
                    )
                    if mode_unknown:
                        raise ValueError(
                            f"freq_result.modes carries field(s) "
                            f"{sorted(mode_unknown)!r} with no reaction-route "
                            f"handling or documented 'not carried' "
                            f"justification. Update "
                            f"_REACTION_MODE_FIELDS_HANDLED or "
                            f"_REACTION_MODE_FIELDS_NOT_CARRIED in "
                            f"adapter.py before this field can silently "
                            f"vanish on the computed-reaction route."
                        )
                calc["freq_frequencies_cm1"] = [m["frequency_cm1"] for m in modes]
                # ``modes``' per-entry ``imaginary_disposition`` (set by
                # the producer's freq-result builder on every non-designated
                # imaginary mode once a reaction coordinate is
                # designated) has no home on the flat per-mode list —
                # the network_pdep/computed-reaction shape carries it
                # separately, keyed by mode_index, via
                # ``CalculationIn.freq_imaginary_dispositions``
                # (shared/calculation_in.py). Drop it and it silently
                # reverts to "undeclared", which is exactly the state
                # stationary_point.py's ambiguity check blocks on.
                dispositions = {
                    m["mode_index"]: m["imaginary_disposition"]
                    for m in modes
                    if m.get("imaginary_disposition") is not None
                }
                if dispositions:
                    calc["freq_imaginary_dispositions"] = dispositions


def flatten_all_reaction_calcs(bundle: dict[str, Any]) -> None:
    """Walk a computed-reaction bundle and flatten every calc in place.

    Hits each species block's primary opt (under ``conformers[*].calculation``)
    and additionals (under ``calculations``), then the TS's primary
    (``transition_state.calculation``) and additionals
    (``transition_state.calculations``). One pass at the bundle root
    keeps the per-builder code free of shape concerns.
    """
    for species in bundle.get("species") or []:
        for conf in species.get("conformers") or []:
            calc = conf.get("calculation")
            if isinstance(calc, dict):
                flatten_result_fields(calc)
        for calc in species.get("calculations") or []:
            if isinstance(calc, dict):
                flatten_result_fields(calc)
    ts = bundle.get("transition_state")
    if isinstance(ts, dict):
        primary = ts.get("calculation")
        if isinstance(primary, dict):
            flatten_result_fields(primary)
        for calc in ts.get("calculations") or []:
            if isinstance(calc, dict):
                flatten_result_fields(calc)
