"""The key namespace is the producer's, passed in; the shared composition is stable."""

import re

import pytest

from tckdb_core.idempotency import (
    ArtifactIdempotencyInputs,
    IdempotencyInputs,
    build_artifact_idempotency_key,
    build_idempotency_key,
)

SERVER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,200}$")


def _inputs():
    return IdempotencyInputs.from_payload(
        project_label="proj", species_label="CH4", conformer_label="c0",
        payload_kind="conformer_calculation", payload={"a": 1},
    )


def test_the_namespace_is_required():
    with pytest.raises(TypeError):
        build_idempotency_key(_inputs())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_artifact_idempotency_key(  # type: ignore[call-arg]
            ArtifactIdempotencyInputs("p", "s", None, "output_log", "0" * 64, "calc_x"))


def test_two_producers_never_share_a_key():
    inputs = _inputs()
    assert build_idempotency_key(inputs, namespace="arc") != build_idempotency_key(inputs, namespace="rmg")
    assert build_idempotency_key(inputs, namespace="rmg").startswith("rmg:proj:CH4:c0:")


def test_artifact_key_shape_and_server_pattern():
    key = build_artifact_idempotency_key(
        ArtifactIdempotencyInputs("proj", "CH4", None, "output_log", "ab" * 32, "calc_abc"),
        namespace="rmg",
    )
    assert key == "rmg:proj:CH4:artifact:calc_abc:output_log:" + "ab" * 8
    assert SERVER_PATTERN.match(key)
