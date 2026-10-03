"""Pytest configuration for the tckdb_arc test suite.

Puts the tests directory on ``sys.path`` so cross-test helper imports
(``from test_adapter import _reaction_output_doc``) resolve regardless of the
invocation cwd — mirroring the Chemkin adapter's conftest convenience.

Also checks every whole request the adapter builds with its route's published
pydantic model and the producer contract's JSON Schema. The mechanism is
:func:`tckdb_core.testing.contract_hook.contract_checks_fixture`; this file only
names ARC's builders.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from tckdb_core.testing.contract_hook import (  # noqa: E402  (needs the sys.path entry above)
    REFUSED_MARKER as _REFUSED_MARKER,  # noqa: F401
    contract_checks_fixture,
    register_markers,
)

from _contract import ROUTE_SCHEMAS  # noqa: E402
from tckdb_arc import adapter as adapter_module  # noqa: E402

# Every TCKDBAdapter method that returns a whole upload request, and the
# contract surface of the route it is posted to.
_PAYLOAD_BUILDERS = {
    "_build_payload": ROUTE_SCHEMAS[adapter_module.CONFORMER_UPLOAD_ENDPOINT],
    "_build_computed_species_payload": ROUTE_SCHEMAS[adapter_module.COMPUTED_SPECIES_ENDPOINT],
    "_build_computed_reaction_payload": ROUTE_SCHEMAS[adapter_module.COMPUTED_REACTION_ENDPOINT],
    "_compose_transition_state_request": ROUTE_SCHEMAS[adapter_module.TRANSITION_STATE_ENDPOINT],
}


def pytest_configure(config):
    register_markers(config)


_contract_checks_every_built_payload = contract_checks_fixture(
    adapter_cls=adapter_module.TCKDBAdapter,
    payload_builders=_PAYLOAD_BUILDERS,
    artifact_batch_method="_upload_artifact_batch",
    artifact_schema_name=ROUTE_SCHEMAS[adapter_module.ARTIFACTS_ENDPOINT_TEMPLATE],
)
