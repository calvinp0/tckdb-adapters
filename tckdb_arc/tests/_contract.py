"""Check payloads against the producer contract tckdb-schemas ships.

The implementation moved to :mod:`tckdb_core.testing.contract` (batch L2) so every
producer adapter's tests share one copy; this module re-exports it so
``from _contract import contract_validate`` keeps working in this suite. The
conftest fixture ``_contract_checks_every_built_payload`` (built by
:func:`tckdb_core.testing.contract_hook.contract_checks_fixture` over this adapter's
own builders) applies the check to every built request; tests call
:func:`contract_validate` where they check a payload or fragment themselves.

``TARGET_SCHEMAS_LINE`` lives in :mod:`tckdb_core.testing.contract` (the one line
``tools/tckdb_drift.py --bump`` rewrites); ``test_contract_pin`` here and its twin in
``tckdb_core/tests`` both pin it.
"""

from tckdb_core.testing.contract import (  # noqa: F401
    REQUEST_MODELS,
    ROUTE_SCHEMAS,
    TARGET_SCHEMAS_LINE,
    artifacts_request_bodies,
    assert_fragment_matches_contract,
    assert_matches_contract,
    assert_request_is_accepted,
    contract_validate,
    installed_schemas_version,
)
