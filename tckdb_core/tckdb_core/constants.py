"""TCKDB route endpoints, payload kinds and enum values shared by every producer.

These name the routes of the TCKDB upload contract and the retry policy of the
readiness probe; none of it depends on which tool produced the data.
"""

CONFORMER_UPLOAD_ENDPOINT = "/uploads/conformers"
PAYLOAD_KIND = "conformer_calculation"

ARTIFACTS_ENDPOINT_TEMPLATE = "/calculations/{calculation_id}/artifacts"

# Computed-species bundle endpoint. One self-contained payload that
# carries species_entry + conformers + calcs + artifacts + thermo, with
# all cross-references expressed as local string keys (no DB ids).
COMPUTED_SPECIES_ENDPOINT = "/uploads/computed-species"
COMPUTED_SPECIES_KIND = "computed_species"

# Computed-reaction bundle endpoint. Like computed-species but adds
# reactant/product species blocks, an inline transition_state, and a
# modified-Arrhenius kinetics block with producer-declared
# source_calculations.
COMPUTED_REACTION_ENDPOINT = "/uploads/computed-reaction"
COMPUTED_REACTION_KIND = "computed_reaction"

# Standalone transition-state endpoint. One payload per converged TS,
# carrying the embedded reaction (reactants/products by identity, family,
# reversibility), the saddle-point geometry, a required primary opt, and
# optional freq/sp/irc/path_search calculations. Unlike the computed-
# reaction bundle, no species minima or kinetics travel with it — the TS
# is the record. See ``submit_computed_ts_from_output``.
TRANSITION_STATE_ENDPOINT = "/uploads/transition-states"
TRANSITION_STATE_KIND = "transition_state"

# Readiness-probe (/readyz) retry policy. During a long producer run the TCKDB
# server can be briefly not-ready (restart, rolling deploy, momentary
# load). Rather than fail the in-run upload on the first blip, the
# preflight probe retries with exponential backoff before giving up.
#
# PREFLIGHT_MAX_ATTEMPTS counts the initial probe plus retries (5 → 1
# initial + 4 retries). Delays follow PREFLIGHT_BASE_DELAY_SECONDS * 2**i
# (1s, 2s, 4s, 8s) capped at PREFLIGHT_MAX_DELAY_SECONDS, so the worst
# case waits ~1+2+4+8 = 15s across the four gaps — well under a minute so
# a genuinely-down server doesn't stall the whole run. Tunable here.
PREFLIGHT_MAX_ATTEMPTS = 5
PREFLIGHT_BASE_DELAY_SECONDS = 1.0

PREFLIGHT_MAX_DELAY_SECONDS = 8.0

# TCKDB's ``CalculationWithResultsPayload.origin_kind`` is a *validated*
# enum. The backend hoists ``parameters_json.tckdb_origin.origin_kind``
# into that top-level field (that is why the ``reused_result`` marker,
# which happens to be a valid member, round-trips cleanly), so any value
# a producer emits for ``origin_kind`` — even nested under the "free-form"
# parameters_json — MUST be one of these. Producer-specific provenance detail
# that is NOT an enum member (e.g. "screened_conformer") is carried on a
# separate ``origin_detail`` key, which the backend leaves opaque.
VALID_TCKDB_ORIGIN_KINDS: frozenset[str] = frozenset(
    {"executed", "reused_result", "imported", "derived"}
)

# Backend ``KIND_ALLOWED_EXTENSIONS`` for ``output_log`` is
# ``{.log, .out, .orca}`` (see TCKDB_v2 fragments/artifact.py).
# Non-traditional log artifacts (e.g. the GSM ``stringfile.xyz0000``)
# need their declared filename adapted to satisfy the allowlist while
# preserving the original basename for human inspection. The backend
# treats ``filename`` as provenance metadata only — storage is content-
# addressed and the filename does not influence the URI.
_OUTPUT_LOG_ALLOWED_EXTS: frozenset[str] = frozenset({".log", ".out", ".orca"})
