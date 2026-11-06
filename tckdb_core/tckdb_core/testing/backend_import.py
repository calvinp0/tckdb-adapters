"""Import TCKDB's backend identity rules for the replica comparison tests.

``TCKDB_BACKEND_PATH`` names a TCKDB ``backend/`` directory at the revision the
suites are pinned to (``tckdb-pin.toml``); CI clones it (see ``ci.yml``). The pure
``app.chemistry`` modules import without the backend's dependencies; the full
``app.services.calculation_resolution`` needs fastapi and sqlalchemy and is used
when available. ``TCKDB_REQUIRE_BACKEND=1`` turns "not available" into a failure
(CI sets it) so the comparison cannot silently stop running.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest


def _unavailable(reason: str):
    if os.environ.get("TCKDB_REQUIRE_BACKEND") == "1":
        pytest.fail(f"TCKDB_REQUIRE_BACKEND=1 but {reason}")
    pytest.skip(reason)


def backend_modules():
    """Return the backend's pure identity-key modules (skip/fail when absent)."""
    backend = os.environ.get("TCKDB_BACKEND_PATH")
    if not backend:
        _unavailable("set TCKDB_BACKEND_PATH to TCKDB's backend/ to compare against its hash")
    sys.path.insert(0, backend)
    try:
        from app.chemistry import basis_set_names, dispersion_names, lot_component_names, method_names
    except ImportError as exc:
        _unavailable(f"backend identity modules not importable from {backend}: {exc}")
    finally:
        sys.path.remove(backend)
    return basis_set_names, dispersion_names, lot_component_names, method_names


def real_backend_level_hash():
    """The backend's real ``_level_of_theory_hash`` wrapped to take a level dict, or ``None`` with no fallback.

    Returns ``(function, None)`` when ``app.services.calculation_resolution`` (needs the backend's
    fastapi and sqlalchemy, and ``tckdb_schemas``) imports from ``TCKDB_BACKEND_PATH``; else
    ``(None, reason)``. Unlike :func:`backend_level_hash` it never substitutes the replica of the
    payload rules, so a test that uses it compares the adapter against the server's own function.
    """
    backend = os.environ.get("TCKDB_BACKEND_PATH")
    if not backend:
        return None, "TCKDB_BACKEND_PATH is not set"
    sys.path.insert(0, backend)
    try:
        from app.services.calculation_resolution import _level_of_theory_hash
        from tckdb_schemas.fragments.refs import LevelOfTheoryRef
    except ImportError as exc:
        return None, (
            f"the real backend _level_of_theory_hash is not importable from {backend} ({exc}); "
            "run it in an environment with the backend's dependencies (fastapi, sqlalchemy) and tckdb_schemas")
    finally:
        sys.path.remove(backend)
    return (lambda level: _level_of_theory_hash(LevelOfTheoryRef(**level))), None


def backend_level_hash(level: dict) -> str:
    """The backend's hash of ``level``: the real function when importable, else the
    same payload built from the backend's own identity keys (a replica of the payload
    rules; use :func:`real_backend_level_hash` to require the real function)."""
    basis_set_names, dispersion_names, lot_component_names, _ = backend_modules()
    backend = os.environ["TCKDB_BACKEND_PATH"]
    sys.path.insert(0, backend)
    try:
        from app.services.calculation_resolution import _level_of_theory_hash
        from tckdb_schemas.fragments.refs import LevelOfTheoryRef

        return _level_of_theory_hash(LevelOfTheoryRef(**level))
    except ImportError:
        pass
    finally:
        sys.path.remove(backend)
    method_key, dispersion_key = dispersion_names.level_identity_keys(level["method"], level.get("dispersion"))
    payload = {
        "method": method_key,
        "basis": basis_set_names.basis_identity_key(level.get("basis")),
        "aux_basis": basis_set_names.basis_identity_key(level.get("aux_basis")),
        "cabs_basis": basis_set_names.basis_identity_key(level.get("cabs_basis")),
        "dispersion": dispersion_key,
        "solvent": lot_component_names.component_identity_key(level.get("solvent")),
        "solvent_model": lot_component_names.component_identity_key(level.get("solvent_model")),
        "spin_treatment": level.get("spin_treatment") or "unknown",
    }
    for name in ("core_treatment", "relativistic_hamiltonian", "pno_truncation", "ri_approximation"):
        if level.get(name) is not None:
            payload[name] = level[name]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
