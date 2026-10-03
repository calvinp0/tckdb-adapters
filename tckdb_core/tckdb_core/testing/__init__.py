"""Test kit for producer adapters built on ``tckdb_core``.

Importable by any adapter's test suite (it needs ``pytest`` and ``jsonschema``, the
``test`` extra, and is never imported by the runtime package):

* :mod:`~tckdb_core.testing.contract`: validate payloads against the producer contract
  tckdb-schemas ships (``contract_validate``, ``REQUEST_MODELS``, ``TARGET_SCHEMAS_LINE``).
* :mod:`~tckdb_core.testing.contract_hook`: the autouse fixture that validates every request
  an adapter builds, registered against the adapter's own builder names.
* :mod:`~tckdb_core.testing.backend_import`: import TCKDB's backend identity rules for the
  replica comparison tests.
* :mod:`~tckdb_core.testing.live`: scaffolding for an opt-in live gate against an isolated
  local TCKDB backend.
"""
