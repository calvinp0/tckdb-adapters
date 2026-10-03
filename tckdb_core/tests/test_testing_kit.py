"""The test kit other adapters' suites import: contract checks, the contract hook, the live-gate guards."""

import pytest

from tckdb_core.testing import contract, live
from tckdb_core.testing.backend_import import backend_modules  # noqa: F401  (importable by an adapter's tests)


def test_contract_validate_runs_the_model_then_the_schema():
    from tckdb_schemas.workflows.conformer_upload import ConformerUploadRequest

    with pytest.raises(ValueError):
        contract.contract_validate(ConformerUploadRequest, {})
    assert set(contract.REQUEST_MODELS) == {
        "ConformerUploadRequest", "ComputedSpeciesUploadRequest",
        "ComputedReactionUploadRequest", "TransitionStateUploadRequest"}


def test_artifact_bodies_group_by_calculation(tmp_path):
    from types import SimpleNamespace

    one, two = tmp_path / "a.log", tmp_path / "b.log"
    one.write_text("a")
    two.write_text("b")
    items = [SimpleNamespace(kind="output_log", path=str(one), calculation_id=1, sha256="x", bytes=1),
             SimpleNamespace(kind="input", path=str(two), calculation_id=1),
             SimpleNamespace(kind="output_log", path=str(two), calculation_id=2)]
    bodies = contract.artifacts_request_bodies(items)
    assert [len(b["artifacts"]) for b in bodies] == [2, 1]
    assert bodies[0]["artifacts"][0]["filename"] == "a.log" and bodies[0]["artifacts"][0]["sha256"] == "x"


def test_the_loopback_guard_and_commit_probe():
    assert live.assert_loopback_url("http://127.0.0.1:58010/api/v1")
    for bad in ("https://tckdb.example.org/api/v1", "http://127.0.0.1:8010/api/v1"):
        with pytest.raises(ValueError):
            live.assert_loopback_url(bad)
    assert live.assert_integration_backend(
        {"components": {"artifact_storage": {"bucket": "tckdb-integ-artifacts"}}}) == "tckdb-integ-artifacts"
    with pytest.raises(ValueError):
        live.assert_integration_backend({"components": {"artifact_storage": {"bucket": "tckdb-artifacts"}}})
    assert live.commit_probe({"primary_calculation": {"calculation_id": 7}}) == "/calculations/7"
    assert live.commit_probe({"calculation_keys": {"a": 9, "b": 4}}) == "/calculations/4"
    assert live.commit_probe("no") is None


def test_warning_code_registry_type():
    from tckdb_core.warning_codes import CodedEnum, CoreWarning, registry

    assert CoreWarning.CALCULATION_REF_NOT_RETURNED == "calculation_ref_not_returned"
    assert str(CoreWarning.CALCULATION_REF_NOT_RETURNED) == f"{CoreWarning.CALCULATION_REF_NOT_RETURNED}" == \
        "calculation_ref_not_returned"
    assert type(CoreWarning.CALCULATION_REF_NOT_RETURNED.value) is str
    assert issubclass(CoreWarning, CodedEnum)
    rows = list(registry(CoreWarning))
    assert rows[0][0] == "calculation_ref_not_returned" and "\n" not in rows[0][1]


def test_the_warning_dict_for_a_core_code_is_unchanged():
    from tckdb_core.uploader import _calculation_ref_not_returned_warning

    assert _calculation_ref_not_returned_warning(5, producer="demo") == {
        "code": "calculation_ref_not_returned",
        "message": ("The upload response carried no calculation_ref for calculation 5, "
                    "so its artifacts were posted to the deprecated integer-id path and the "
                    "idempotency key was built from that id."),
        "field": "calculation_ref",
        "context": {"source": "demo_self_check", "calculation_id": 5},
    }
    assert type(_calculation_ref_not_returned_warning(5, producer="demo")["code"]) is str
