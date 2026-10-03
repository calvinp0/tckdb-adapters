"""Import TCKDB's backend identity rules for the replica comparison tests.

``TCKDB_BACKEND_PATH`` names a TCKDB ``backend/`` directory at the revision the
suite is pinned to (``tckdb-pin.toml``); CI clones it (see ``ci.yml``). The pure
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


def backend_level_hash(level: dict) -> str:
    """The backend's hash of ``level``: the real function when importable, else the
    same payload built from the backend's own identity keys."""
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
        "keywords": level.get("keywords"),
        "spin_treatment": level.get("spin_treatment") or "unknown",
    }
    if level.get("core_treatment") is not None:
        payload["core_treatment"] = level["core_treatment"]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
