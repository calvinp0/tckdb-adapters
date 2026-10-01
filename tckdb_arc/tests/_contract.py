"""Check payloads against the producer contract tckdb-schemas ships.

``tckdb_schemas.contract`` carries one JSON Schema per upload payload,
generated from TCKDB's own routes and models (tckdb-schemas 0.52+). Every
payload the adapter builds is validated against the schema for its route,
as the JSON the client sends (a ``json.dumps`` round trip, NaN refused), so
a payload the contract refuses fails the test that built it. The conftest
fixture ``_contract_checks_every_built_payload`` applies this, after the
route's published pydantic model, to every adapter builder; tests call
:func:`contract_validate` where they check a payload or fragment themselves.

What this does not cover: the route handlers' own rules (ownership and
role checks such as ``thermo_energy_level_*``, composition checks such as
``calculation_geometry_composition_mismatch``), request headers, and JSON
Schema ``format`` keywords (no format checker is enabled). The live
integration gate (``tests/integration``) covers the handlers.

The suite targets one tckdb-schemas line, :data:`TARGET_SCHEMAS_LINE`. A
different installed version fails ``test_contract_pin`` rather than
silently testing against a contract nobody read.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.metadata import version
from typing import Any

from jsonschema import Draft202012Validator
from tckdb_schemas import contract
from tckdb_schemas.fragments.artifact import ArtifactIn
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.computed_species_upload import ComputedSpeciesUploadRequest
from tckdb_schemas.workflows.conformer_upload import ConformerUploadRequest
from tckdb_schemas.workflows.transition_state_upload import TransitionStateUploadRequest

#: The tckdb-schemas release line the suite was run against. Keep it equal
#: to the ``tckdb-schemas`` bound in ``pyproject.toml``; moving it means
#: reading ``python -m tckdb_schemas.contract --since <old>`` first.
TARGET_SCHEMAS_LINE = (0, 64)

#: Adapter upload endpoints and the contract surface each one posts.
ROUTE_SCHEMAS = {
    "/uploads/conformers": "ConformerUploadRequest",
    "/uploads/computed-species": "ComputedSpeciesUploadRequest",
    "/uploads/computed-reaction": "ComputedReactionUploadRequest",
    "/uploads/transition-states": "TransitionStateUploadRequest",
    "/calculations/{calculation_id}/artifacts": "ArtifactsUploadRequest",
}


#: The published pydantic model of each route's request. The artifact
#: route's request model lives in the backend; its items are ``ArtifactIn``.
REQUEST_MODELS = {
    "ConformerUploadRequest": ConformerUploadRequest,
    "ComputedSpeciesUploadRequest": ComputedSpeciesUploadRequest,
    "ComputedReactionUploadRequest": ComputedReactionUploadRequest,
    "TransitionStateUploadRequest": TransitionStateUploadRequest,
}


def installed_schemas_version() -> str:
    return version("tckdb-schemas")


def _wire_json(payload: Any) -> Any:
    """The payload as the client sends it: JSON, never NaN or Infinity."""
    return json.loads(json.dumps(payload, allow_nan=False))


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    schema = contract.json_schema(schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@lru_cache(maxsize=None)
def _fragment_validator(model_name: str) -> Draft202012Validator:
    """Validator for a nested model, taken from the first route defining it."""
    for route in ROUTE_SCHEMAS.values():
        definitions = contract.json_schema(route).get("$defs", {})
        if model_name in definitions:
            return Draft202012Validator(
                {"$defs": definitions, "$ref": f"#/$defs/{model_name}"}
            )
    raise KeyError(
        f"{model_name!r} is neither a shipped payload schema nor a model any "
        f"adapter route's schema defines"
    )


def _check(validator: Draft202012Validator, payload: Any, name: str) -> None:
    errors = sorted(validator.iter_errors(_wire_json(payload)), key=lambda e: list(e.path))
    if errors:
        detail = "\n".join(
            f"  at {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in errors[:10]
        )
        raise AssertionError(
            f"payload does not match the tckdb-schemas {installed_schemas_version()} "
            f"contract for {name} ({len(errors)} error(s)):\n{detail}"
        )


def assert_matches_contract(payload: Any, schema_name: str) -> None:
    """Validate a whole request against its route's shipped JSON Schema."""
    _check(_validator(schema_name), payload, schema_name)


def assert_fragment_matches_contract(payload: Any, model_name: str) -> None:
    """Validate a nested block against its model's definition in the contract."""
    _check(_fragment_validator(model_name), payload, model_name)


def assert_request_is_accepted(payload: Any, schema_name: str) -> None:
    """Run the route's published pydantic model, then its shipped JSON Schema.

    The model runs its validators (the JSON Schema cannot express them);
    the schema is the contract's own statement of the wire shape. Neither
    reaches the route handlers' database-side rules.
    """
    if schema_name == "ArtifactsUploadRequest":
        for artifact in payload["artifacts"]:
            ArtifactIn.model_validate(artifact)
    else:
        REQUEST_MODELS[schema_name].model_validate(_wire_json(payload))
    assert_matches_contract(payload, schema_name)


def contract_validate(model: Any, payload: Any) -> Any:
    """``model.model_validate(payload)``, then the contract's JSON Schema check.

    The pydantic model runs first, so a test expecting it to refuse still
    sees its error.
    """
    validated = model.model_validate(payload)
    name = model.__name__
    if name in contract.schema_names():
        assert_matches_contract(payload, name)
    else:
        assert_fragment_matches_contract(payload, name)
    return validated


def artifacts_request_bodies(prepared: Any) -> list[dict[str, Any]]:
    """The ``ArtifactsUploadRequest`` bodies an artifact plan becomes.

    Mirrors ``TCKDBClient.upload_artifacts(batch_by_calculation=True)``: one
    body per calculation, each item carrying the kind, filename, base64
    content and the declared sha256/bytes the adapter prepared.
    """
    import base64
    from pathlib import Path

    groups: dict[int, list[dict[str, Any]]] = {}
    for item in prepared:
        artifact = {
            "kind": item.kind,
            "filename": getattr(item, "filename", None) or Path(item.path).name,
            "content_base64": base64.b64encode(Path(item.path).read_bytes()).decode("ascii"),
        }
        if getattr(item, "sha256", None) is not None:
            artifact["sha256"] = item.sha256
        if getattr(item, "bytes", None) is not None:
            artifact["bytes"] = item.bytes
        groups.setdefault(item.calculation_id, []).append(artifact)
    return [{"artifacts": items} for items in groups.values()]
