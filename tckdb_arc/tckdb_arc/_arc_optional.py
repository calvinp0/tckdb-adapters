"""Guarded ``import arc`` boundary — the optional ``[arc]`` extra.

Three payload paths in :mod:`tckdb_arc.adapter` need ARC's ESS parsers, which
are only importable when ARC itself is on the Python path (ARC is a
conda/PYTHONPATH install, not a PyPI package):

* freq-Hessian     — ``arc.parser.parser.determine_ess`` + ``arc.parser.factory.ess_factory``
* IRC trajectory   — ``arc.parser.parser.parse_irc_path`` / ``parse_irc_traj``
* GSM string-file  — ``arc.parser.parser.parse_trajectory`` / ``parse_gsm_stringfile_energies``
                     plus ``arc.species.converter.kabsch`` (mass-weighted RMSD alignment)

Every one of these lives inside a caller ``try/except`` that already degrades
to ``None`` (the sub-payload is simply omitted). The wrappers below raise
:class:`OptionalArcUnavailable` — a subclass of ``ImportError`` — so those
existing ``except`` blocks catch it identically: a base install (no ``[arc]``
extra, ARC not importable) silently omits the Hessian/IRC/GSM sub-payloads
rather than crashing.

The imports are *lazy* (resolved only when a wrapper is actually called), so
importing this module never pulls ARC, and payload paths that don't touch
these three parsers keep working with ARC absent.
"""

from __future__ import annotations


class OptionalArcUnavailable(ImportError):
    """Raised when an ARC-only parser path is exercised without ARC installed.

    Subclasses :class:`ImportError` so callers that ``except ImportError`` (or
    the broad ``except Exception`` the best-effort builders use) degrade to
    ``None`` exactly as they did when the import was a bare ``from arc...``.
    """


_UNAVAILABLE_MSG = (
    "This payload path (Hessian/IRC/GSM re-parse) needs the [arc] extra: "
    "ARC must be importable (conda/PYTHONPATH install). Prefer emitting a "
    "tckdb_evidence sidecar from ARC so a base tckdb-arc install can build it."
)


def require_arc_parser():
    """Return ``(parser, factory, converter)`` ARC modules or raise.

    Raises:
        OptionalArcUnavailable: If ARC (and its ``arc.parser`` /
            ``arc.species.converter`` subpackages) cannot be imported.
    """
    try:
        from arc.parser import parser, factory  # noqa: F401
        from arc.species import converter  # noqa: F401
    except ImportError as exc:
        raise OptionalArcUnavailable(_UNAVAILABLE_MSG) from exc
    return parser, factory, converter


# --- thin lazy wrappers ----------------------------------------------------
# Each resolves ARC only at call time, so importing them is free and the
# caller's existing try/except turns a missing-ARC into an omitted sub-payload.

def determine_ess(*args, **kwargs):
    parser, _factory, _converter = require_arc_parser()
    return parser.determine_ess(*args, **kwargs)


def ess_factory(*args, **kwargs):
    _parser, factory, _converter = require_arc_parser()
    return factory.ess_factory(*args, **kwargs)


def parse_irc_path(*args, **kwargs):
    parser, _factory, _converter = require_arc_parser()
    return parser.parse_irc_path(*args, **kwargs)


def parse_irc_traj(*args, **kwargs):
    parser, _factory, _converter = require_arc_parser()
    return parser.parse_irc_traj(*args, **kwargs)


def parse_trajectory(*args, **kwargs):
    parser, _factory, _converter = require_arc_parser()
    return parser.parse_trajectory(*args, **kwargs)


def parse_gsm_stringfile_energies(*args, **kwargs):
    parser, _factory, _converter = require_arc_parser()
    return parser.parse_gsm_stringfile_energies(*args, **kwargs)


def kabsch(*args, **kwargs):
    _parser, _factory, converter = require_arc_parser()
    return converter.kabsch(*args, **kwargs)
