"""Method-family classification, imported from TCKDB's shipped classifier.

Used for one rule: TCKDB's one-electron Hartree-Fock exemption in the kinetics energy-level
check applies only when the declared level's method is a ``wavefunction`` or ``multireference``
method (so DFT, named composites and unclassified names get none). The server decides that with
``classify_method``, which ships in ``tckdb_schemas.method_families`` since tckdb-schemas 0.174.
The adapter-side replica (the stop-gap) is retired; this module is only the adapter-facing surface.
"""

from __future__ import annotations

from tckdb_schemas.method_families import MethodFamily, classify_method

#: Alias kept for callers that used the replica's name.
Family = MethodFamily

#: Families for which TCKDB exempts a one-electron Hartree-Fock link under the declared level.
ONE_ELECTRON_EXEMPT_FAMILIES = frozenset({MethodFamily.wavefunction, MethodFamily.multireference})


def method_family(name: str | None) -> MethodFamily:
    """The family of a method name; ``unclassified`` for ``None``, blank, or a name the table does not hold."""
    return classify_method(name).family
