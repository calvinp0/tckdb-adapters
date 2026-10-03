"""Names that moved to ``tckdb_core`` still resolve from ``tckdb_arc``, with a DeprecationWarning for the old modules."""

import importlib
import warnings

import pytest

import tckdb_core.constraints
import tckdb_core.level_rules
import tckdb_core.payload_writer

FORWARDED = {
    "tckdb_arc.level_rules": (tckdb_core.level_rules, {
        "DISPERSION_ALIASES", "FOLDED_STEMS", "FOLDED_SUFFIXES", "NAME_ALIASES", "SUFFIX_ALIASES",
        "basis_identity_key", "component_identity_key", "dispersion_identity_key", "level_hash",
        "level_identity_keys", "level_payload", "method_identity_key", "same_level"}),
    "tckdb_arc.payload_writer": (tckdb_core.payload_writer, {
        "ArtifactSidecarMetadata", "BUNDLE_FORMAT_VERSION", "PayloadWriter", "SidecarMetadata",
        "WrittenArtifact", "WrittenPayload", "should_replay_sidecar"}),
}


@pytest.mark.parametrize("old", sorted(FORWARDED))
def test_the_old_public_names_are_forwarded_with_a_deprecation_warning(old):
    shim = importlib.import_module(old)
    core, names = FORWARDED[old]
    for name in sorted(names):
        with pytest.warns(DeprecationWarning, match=f"{old}.{name} moved to {core.__name__}.{name}"):
            value = getattr(shim, name)
        assert value is getattr(core, name)


def test_the_constraints_module_keeps_its_whole_public_api_silently():
    import tckdb_arc.constraints as shim

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert shim.TCKDBCalculationConstraint is tckdb_core.constraints.TCKDBCalculationConstraint
        assert callable(shim.serialize_constraints)


@pytest.mark.parametrize("old,name", [
    ("tckdb_arc.payload_writer", "_SAFE_LABEL"), ("tckdb_arc.payload_writer", "_utcnow_iso"),
    ("tckdb_arc.constraints", "_validate"), ("tckdb_arc.constraints", "_coerce"),
    ("tckdb_arc.level_rules", "_HYPHEN_RULES"), ("tckdb_arc.level_rules", "_SUFFIX_COMPILED"),
    ("tckdb_arc.level_rules", "no_such_name"), ("tckdb_arc.payload_writer", "json"),
    ("tckdb_arc.constraints", "logger"),
])
def test_private_and_unknown_names_are_not_forwarded(old, name):
    with pytest.raises(AttributeError):
        getattr(importlib.import_module(old), name)


def test_the_constraint_serializer_logs_to_the_arc_logger(caplog):
    from tckdb_arc.constraints import serialize_constraints

    with caplog.at_level("WARNING"):
        assert serialize_constraints([{"constraint_kind": "bond", "atoms": [1, 2]}]) == []
    assert [r.name for r in caplog.records] == ["tckdb_arc"]


def test_the_names_arcs_own_repository_imports_are_unchanged():
    from tckdb_arc import TCKDBAdapter, TCKDBConfig, run_upload_sweep  # noqa: F401
    from tckdb_arc.adapter import TCKDBAdapter as Adapter  # noqa: F401
    from tckdb_arc.config import TCKDBConfig as Config
    from tckdb_arc.idempotency import IDEMPOTENCY_NAMESPACE, build_idempotency_key  # noqa: F401
    from tckdb_arc.sweep import run_upload_sweep as sweep  # noqa: F401

    assert Config.from_dict(None) is None
    assert IDEMPOTENCY_NAMESPACE == "arc"


def test_the_imported_core_is_the_sibling_of_the_imported_arc():
    import pathlib

    import tckdb_arc.adapter
    arc_dir = pathlib.Path(tckdb_arc.adapter.__file__).resolve().parent.parent
    core_dir = pathlib.Path(tckdb_core.constraints.__file__).resolve().parent.parent
    assert core_dir == arc_dir.parent / "tckdb_core"
