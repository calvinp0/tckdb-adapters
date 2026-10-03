"""Pin the tckdb-schemas contract the core's own suite validates payloads against.

The twin of ``tckdb_arc/tests/test_contract_pin.py``: both read the one
``TARGET_SCHEMAS_LINE`` in :mod:`tckdb_core.testing.contract`, so a bump that moves it
(``tools/tckdb_drift.py --bump``) is checked against every package's ``pyproject.toml``.
A silent upgrade or downgrade of tckdb-schemas fails here, naming the version, until
someone reads ``python -m tckdb_schemas.contract --since <old>`` and moves the pin and
the bound together.
"""

import re
import tomllib
from pathlib import Path

from tckdb_schemas import contract

from tckdb_core import constants
from tckdb_core.testing.contract import ROUTE_SCHEMAS, TARGET_SCHEMAS_LINE, installed_schemas_version

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


def test_every_core_route_constant_has_a_shipped_schema():
    posted = {
        constants.CONFORMER_UPLOAD_ENDPOINT,
        constants.COMPUTED_SPECIES_ENDPOINT,
        constants.COMPUTED_REACTION_ENDPOINT,
        constants.TRANSITION_STATE_ENDPOINT,
        constants.ARTIFACTS_ENDPOINT_TEMPLATE,
    }
    assert posted == set(ROUTE_SCHEMAS)
    assert set(ROUTE_SCHEMAS.values()) <= set(contract.schema_names())
