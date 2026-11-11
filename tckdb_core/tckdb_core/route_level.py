"""Compare an ESS route line with the level of theory a calculation claims.

The route is what ran (observed); the level is what was requested. TCKDB
refuses nothing here, but a producer that states both can notice that they name
different methods or bases and report it instead of filing the request's level for
a job that ran at another one.

The parser is Gaussian-only. Callers must compare a route only when the record states
the job ran in Gaussian (``ess_software[<job>]``). An ORCA keyword line carries slash
tokens that are not a ``method/basis`` pair (auxiliary basis sets such as
``aug-cc-pvtz/c``, ``def2/J``, ``def2-TZVP/C``, ``def2/JK``), and ARC exports ORCA routes
without the leading ``!``, so the text alone cannot tell the programs apart.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

ROUTE_PAIR_RE = re.compile(r"^([^/=\s]+)/([^/=\s]+)$")


def norm_method(text: str) -> str:
    return text.lower().replace("-", "").replace("_", "").replace(" ", "")


def norm_basis(text: str) -> str:
    out = text.lower().replace("(d,p)", "**").replace("(d)", "*")
    return out.replace("-", "").replace("_", "").replace(" ", "")


def route_method_basis(route: Any) -> tuple[str, str] | None:
    """The one ``method/basis`` token of a Gaussian-style route line, or ``None``.

    Only an unambiguous line counts: exactly one whitespace-delimited token of
    the form ``a/b`` with no ``=``, and the line is not a composite route
    (``#CBS-QB3 opt freq``). An Orca line is not supported: its auxiliary basis sets
    carry a slash (``def2/J``), so the caller must not pass one. A Gaussian
    density-fitting set written as a second ``a/b`` token (``b3lyp/def2tzvp def2tzvp/auto``)
    makes the line ambiguous, and ``b3lyp/def2tzvp/auto`` is one token with two slashes that
    the pair pattern does not match; both give ``None`` (no contradiction is reported).
    """
    if not isinstance(route, str):
        return None
    pairs = []
    for token in route.split():
        match = ROUTE_PAIR_RE.match(token)
        if match:
            pairs.append(match.groups())
    return pairs[0] if len(pairs) == 1 else None


def route_contradicts_level(route: Any, level: Mapping[str, Any] | None) -> str | None:
    """A description when ``route`` clearly names another method or basis than ``level``, else ``None``.

    The route is what ran (observed); the level is what was requested. A Gaussian
    ``u`` or ``ro`` prefix on the route's method is the spin treatment, not the
    method (``ub3lyp`` is ``b3lyp``). Only a level with a method and a basis is
    compared; a dispersion carried in the level's method, or a route that could not be read, is never flagged.
    """
    pair = route_method_basis(route)
    if pair is None or not isinstance(level, Mapping):
        return None
    method, basis = level.get("method"), level.get("basis")
    if not method or not basis:
        return None
    r_method, r_basis = norm_method(pair[0]), norm_basis(pair[1])
    l_method, l_basis = norm_method(str(method)), norm_basis(str(basis))
    candidates = {r_method}
    for prefix in ("ro", "u", "r"):
        if r_method.startswith(prefix):
            candidates.add(r_method[len(prefix):])
    if l_method not in candidates or r_basis != l_basis:
        return f"route {pair[0]}/{pair[1]} vs level {method}/{basis}"
    return None
