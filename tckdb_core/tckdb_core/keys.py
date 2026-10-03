"""Bundle-local key helpers.

Local string keys tie the parts of a self-contained bundle to each other
(geometry -> calculation -> thermo/kinetics). TCKDB never sees them as
identities; they only have to be deterministic and unique within one bundle.
"""

import re

_KEY_PART_RE = re.compile(r"[^A-Za-z0-9]+")


def _safe_key_part(label: str | None) -> str:
    """Sanitize a label for use as part of a bundle local key.

    Bundle keys ride into ``GeometryIn.key`` / ``CalculationIn.key``
    where the schema only requires ``min_length=1``, but downstream
    consumers (and the idempotency-key sanitizer) prefer
    ``[A-Za-z0-9._:-]``. We keep alphanumerics, drop everything else,
    cap to 32 chars, and fall back to ``"x"`` for an all-junk label so
    we never produce an empty key segment.
    """
    if not label:
        return "x"
    cleaned = _KEY_PART_RE.sub("", str(label))[:32]
    return cleaned or "x"


def _local_key_for_actor(prefix: str, index: int, label: str | None) -> str:
    """Build a deterministic per-actor *species* key like ``"r0_CHO"`` / ``"p1_CH3"``.

    The numeric ``index`` is what guarantees uniqueness across actors
    that happen to share a chemical label (e.g. H + H ⇌ H2 has two
    ``r*_H`` slots). The sanitized label tail is purely informational —
    a human reading the JSON should be able to tell ``r0_CHO`` from
    ``r1_CH4`` without cross-referencing. Calc keys derive from
    :func:`_calc_prefix_for_actor` instead, which omits the label so
    calc keys stay short (e.g. ``r0_opt`` not ``r0_CHO_opt``).
    """
    return f"{prefix}{index}_{_safe_key_part(label)}"


def _calc_prefix_for_actor(prefix: str, index: int) -> str:
    """Build the per-actor calc-key prefix (e.g. ``"r0"`` / ``"p1"``).

    The chemical label is intentionally omitted: calculation keys ride
    into ``kinetics.source_calculations`` and are referenced verbatim,
    so shorter is better as long as uniqueness is preserved (the
    role-letter + index combo guarantees it).
    """
    return f"{prefix}{index}"
