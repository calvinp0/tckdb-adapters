"""Check every request an adapter builds with its route's published model and JSON Schema.

An adapter's ``conftest.py`` registers the hook against its own builder names::

    from tckdb_core.testing.contract import ROUTE_SCHEMAS
    from tckdb_core.testing.contract_hook import contract_checks_fixture, register_markers

    _contract_checks_every_built_payload = contract_checks_fixture(
        adapter_cls=MyToolAdapter,
        payload_builders={
            "_build_payload": ROUTE_SCHEMAS[CONFORMER_UPLOAD_ENDPOINT],
            ...
        },
        artifact_batch_method="_upload_artifact_batch",
    )

    def pytest_configure(config):
        register_markers(config)

Each listed method of ``adapter_cls`` is wrapped for the duration of a test: the request it
returns is run through :func:`~tckdb_core.testing.contract.assert_request_is_accepted`
(pydantic model first, then the JSON Schema), so a request the contract refuses fails the
test that built it. A test marked ``payload_refused_by_contract`` deliberately builds a
request TCKDB refuses (the adapter passes a bad reference through instead of repairing it);
the hook then requires at least one refusal instead of an acceptance.

It does not run the route handlers' rules (``thermo_energy_level_*``,
``calculation_geometry_composition_mismatch``), check headers, or enforce JSON Schema
``format`` keywords; only the live gate (:mod:`tckdb_core.testing.live`) reaches those.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import Any

import pytest

from tckdb_core.testing.contract import artifacts_request_bodies, assert_request_is_accepted

REFUSED_MARKER = "payload_refused_by_contract"


def register_markers(config) -> None:
    """Register the ``payload_refused_by_contract`` marker (call from ``pytest_configure``)."""
    config.addinivalue_line(
        "markers",
        f"{REFUSED_MARKER}: the test deliberately builds a request TCKDB refuses "
        "(the adapter passes a bad reference through instead of repairing it); "
        "the contract hook then requires a refusal instead of an acceptance.",
    )


def checked_builder(builder, schema_name: str, refusals: list | None):
    """``builder`` with its returned request checked against ``schema_name``."""
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


def checked_artifact_batch(upload_batch, schema_name: str):
    """An artifact-batch uploader with every request body it would post checked first."""
    @functools.wraps(upload_batch)
    def upload(self, *, prepared, **kwargs):
        for body in artifacts_request_bodies(prepared):
            assert_request_is_accepted(body, schema_name)
        return upload_batch(self, prepared=prepared, **kwargs)
    return upload


def contract_checks_fixture(
    *,
    adapter_cls: type,
    payload_builders: Mapping[str, str],
    artifact_batch_method: str | None = None,
    artifact_schema_name: str = "ArtifactsUploadRequest",
    name: str = "_contract_checks_every_built_payload",
) -> Any:
    """The autouse fixture that checks every request ``adapter_cls`` builds.

    ``payload_builders`` maps each ``adapter_cls`` method that returns a whole upload request
    to the contract surface (schema name) of the route it is posted to;
    ``artifact_batch_method`` names the method that posts artifact batches, if any.
    """
    @pytest.fixture(autouse=True, name=name)
    def _contract_checks(monkeypatch, request):
        refusals = [] if request.node.get_closest_marker(REFUSED_MARKER) else None
        for method, schema_name in payload_builders.items():
            monkeypatch.setattr(
                adapter_cls, method,
                checked_builder(getattr(adapter_cls, method), schema_name, refusals))
        if artifact_batch_method is not None:
            monkeypatch.setattr(
                adapter_cls, artifact_batch_method,
                checked_artifact_batch(getattr(adapter_cls, artifact_batch_method),
                                       artifact_schema_name))
        yield
        if refusals is not None:
            assert refusals, (
                f"marked {REFUSED_MARKER}, but every request it built was accepted")
    return _contract_checks
