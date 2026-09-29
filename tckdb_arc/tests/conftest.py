"""Pytest configuration for the tckdb_arc test suite.

Puts the tests directory on ``sys.path`` so cross-test helper imports
(``from test_adapter import _reaction_output_doc``) resolve regardless of the
invocation cwd — mirroring the Chemkin adapter's conftest convenience.

Also checks every whole request the adapter builds with its route's published
pydantic model and the producer contract's JSON Schema (see ``_contract.py``).
"""

import functools
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from _contract import (  # noqa: E402  (needs the sys.path entry above)
    ROUTE_SCHEMAS,
    artifacts_request_bodies,
    assert_request_is_accepted,
)
from tckdb_arc import adapter as adapter_module  # noqa: E402

# Every TCKDBAdapter method that returns a whole upload request, and the
# contract surface of the route it is posted to.
_PAYLOAD_BUILDERS = {
    "_build_payload": ROUTE_SCHEMAS[adapter_module.CONFORMER_UPLOAD_ENDPOINT],
    "_build_computed_species_payload": ROUTE_SCHEMAS[adapter_module.COMPUTED_SPECIES_ENDPOINT],
    "_build_computed_reaction_payload": ROUTE_SCHEMAS[adapter_module.COMPUTED_REACTION_ENDPOINT],
    "_compose_transition_state_request": ROUTE_SCHEMAS[adapter_module.TRANSITION_STATE_ENDPOINT],
}


_REFUSED_MARKER = "payload_refused_by_contract"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{_REFUSED_MARKER}: the test deliberately builds a request TCKDB refuses "
        "(the adapter passes a bad reference through instead of repairing it); "
        "the contract hook then requires a refusal instead of an acceptance.",
    )


def _checked_builder(builder, schema_name, refusals):
    @functools.wraps(builder)
    def build(*args, **kwargs):
        payload = builder(*args, **kwargs)
        if refusals is None:
            assert_request_is_accepted(payload, schema_name)
        else:
            try:
                assert_request_is_accepted(payload, schema_name)
            except (AssertionError, ValueError) as exc:  # pydantic errors are ValueErrors
                refusals.append(exc)
        return payload
    return build


def _checked_artifact_batch(upload_batch):
    schema_name = ROUTE_SCHEMAS[adapter_module.ARTIFACTS_ENDPOINT_TEMPLATE]

    @functools.wraps(upload_batch)
    def upload(self, *, prepared, **kwargs):
        for body in artifacts_request_bodies(prepared):
            assert_request_is_accepted(body, schema_name)
        return upload_batch(self, prepared=prepared, **kwargs)
    return upload


@pytest.fixture(autouse=True)
def _contract_checks_every_built_payload(monkeypatch, request):
    """Fail any test whose adapter-built request the model or schema refuses.

    A test marked ``payload_refused_by_contract`` must instead see at least
    one built request refused.
    """
    refusals = [] if request.node.get_closest_marker(_REFUSED_MARKER) else None
    cls = adapter_module.TCKDBAdapter
    for name, schema_name in _PAYLOAD_BUILDERS.items():
        monkeypatch.setattr(
            cls, name, _checked_builder(getattr(cls, name), schema_name, refusals))
    monkeypatch.setattr(
        cls, "_upload_artifact_batch", _checked_artifact_batch(cls._upload_artifact_batch),
    )
    yield
    if refusals is not None:
        assert refusals, (
            f"marked {_REFUSED_MARKER}, but every request it built was accepted")
