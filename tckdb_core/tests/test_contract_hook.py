"""The contract hook an adapter's conftest registers against its own builder names."""

import pytest

from tckdb_core.testing.contract_hook import REFUSED_MARKER, contract_checks_fixture


class _Adapter:
    """A producer whose only builder returns a request the contract refuses."""

    def build(self):
        return {"not": "a conformer upload"}

    def upload_batch(self, *, prepared):
        return prepared


_demo_hook = contract_checks_fixture(
    adapter_cls=_Adapter,
    payload_builders={"build": "ConformerUploadRequest"},
    artifact_batch_method="upload_batch",
    name="_demo_hook",
)


def test_the_hook_fails_a_test_that_builds_a_refused_request():
    with pytest.raises((AssertionError, ValueError)):
        _Adapter().build()


@pytest.mark.payload_refused_by_contract
def test_a_marked_test_may_build_a_refused_request_and_the_hook_requires_the_refusal():
    # No raise: the refusal is recorded, and the fixture's teardown asserts one happened.
    assert _Adapter().build() == {"not": "a conformer upload"}


def test_the_artifact_batch_is_checked_before_it_is_posted(tmp_path):
    from types import SimpleNamespace

    good = tmp_path / "run.log"
    good.write_text("log")
    ok = SimpleNamespace(kind="output_log", path=str(good), calculation_id=1)
    assert _Adapter().upload_batch(prepared=[ok]) == [ok]
    bad = SimpleNamespace(kind="not_a_kind", path=str(good), calculation_id=1)
    with pytest.raises((AssertionError, ValueError)):
        _Adapter().upload_batch(prepared=[bad])


def test_the_marker_is_registered(pytestconfig):
    assert any(line.startswith(REFUSED_MARKER) for line in pytestconfig.getini("markers"))
