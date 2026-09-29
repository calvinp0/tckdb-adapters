"""Pin the tckdb-schemas contract this suite validates payloads against.

The conftest hook checks every built payload against the JSON Schemas the
installed tckdb-schemas ships. That proves conformance only to the contract
that happens to be installed, so these tests pin which one it is: a silent
upgrade (or downgrade) of tckdb-schemas fails here, naming the version, until
someone reads ``python -m tckdb_schemas.contract --since <old>`` and moves
``TARGET_SCHEMAS_LINE`` and the ``pyproject.toml`` bound together.
"""

import re
import tomllib
from pathlib import Path

from tckdb_schemas import contract

from _contract import ROUTE_SCHEMAS, TARGET_SCHEMAS_LINE, installed_schemas_version
from tckdb_arc import adapter as adapter_module

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _release_line(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def test_installed_tckdb_schemas_is_the_targeted_line():
    installed = installed_schemas_version()
    assert _release_line(installed) == TARGET_SCHEMAS_LINE, (
        f"tests ran against tckdb-schemas {installed}, but the suite targets "
        f"{TARGET_SCHEMAS_LINE[0]}.{TARGET_SCHEMAS_LINE[1]}.x. Read "
        f"`python -m tckdb_schemas.contract --since "
        f"{TARGET_SCHEMAS_LINE[0]}.{TARGET_SCHEMAS_LINE[1]}.0` before moving the pin."
    )


def test_pyproject_bounds_tckdb_schemas_to_the_targeted_line():
    dependencies = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    (spec,) = [d for d in dependencies if re.match(r"tckdb-schemas\b", d)]
    major, minor = TARGET_SCHEMAS_LINE
    assert spec.replace(" ", "") == f"tckdb-schemas>={major}.{minor},<{major}.{minor + 1}"


def test_contract_document_is_the_installed_version():
    stamp = re.search(r"tckdb-schemas version `([^`]+)`", contract.markdown())
    assert stamp is not None
    assert stamp.group(1) == installed_schemas_version()


def test_every_adapter_route_has_a_shipped_schema():
    posted = {
        adapter_module.CONFORMER_UPLOAD_ENDPOINT,
        adapter_module.COMPUTED_SPECIES_ENDPOINT,
        adapter_module.COMPUTED_REACTION_ENDPOINT,
        adapter_module.TRANSITION_STATE_ENDPOINT,
        adapter_module.ARTIFACTS_ENDPOINT_TEMPLATE,
    }
    assert posted == set(ROUTE_SCHEMAS)
    assert set(ROUTE_SCHEMAS.values()) <= set(contract.schema_names())
