# Phase 3 implementation brief — versioned ARC evidence sidecar

**Status:** implementation-ready specification  
**Scope:** local code and tests in ARC and `tckdb-adapters`; no deployment or live upload  
**Prerequisites:** Phases 1 and 2 in `PHASE_LOG.md` are complete

## 1. Outcome

Phase 3 removes ARC as a runtime requirement for the last three parser-coupled
scientific-data paths in `tckdb_arc`:

1. Cartesian frequency Hessians;
2. IRC trajectories; and
3. xTB-GSM stringfile geometries and energies.

ARC must parse those artifacts while writing its consolidated output and emit a
versioned, JSON, parser-neutral sidecar. The standalone adapter must use valid
sidecar evidence first. Re-parsing the original files through `_arc_optional.py`
remains only for old or incomplete runs.

The completed behavior is:

```text
new ARC run: output.yml + tckdb_evidence.json
                         |
                         +--> base tckdb-arc (ARC not importable) builds the full data

old ARC run: output.yml only
                         |
                         +--> tckdb-arc[ARC available] re-parses as before
                         +--> base tckdb-arc degrades exactly as Phase 1 does
```

This phase does **not** delete `arc/tckdb/`; that is Phase 4.

## 2. Ground truth and deviations from the re-homing plan

This brief was checked against the current files, not only the planning
document.

- ARC writes `output/output.yml` atomically in
  `arc/output.py::write_output_yml`. It first builds each record through
  `_spc_to_dict`, then writes a temporary file and calls `os.replace`.
- `output.yml` is currently stamped `schema_version: '1.0'`.
- `tckdb_arc.sweep::run_upload_sweep` reads that document, but does not yet
  validate `schema_version` or read any evidence file.
- `TCKDBAdapter` already knows `project_directory`, so evidence should be
  loaded lazily and cached there. Public `submit_*` signatures do not need an
  evidence argument.
- The exact fallback seams are
  `TCKDBAdapter._build_freq_hessian_payload`,
  `TCKDBAdapter._parse_irc_trajectories`, and
  `_build_path_search_result_payload` in `tckdb_arc/adapter.py`.
- GSM is broader than the original plan's shorthand “stringfile
  energies/geometries.” The existing payload also derives Kabsch cumulative
  path distance and reads preserved `gsm_node_outputs` energies/gradients.
  The sidecar must preserve all of that evidence; otherwise a no-ARC build is
  not equivalent.
- ARC is not a real pip dependency. Phase 1 correctly made `[arc]` an empty
  marker and `_arc_optional.py` a guarded `PYTHONPATH`/conda boundary. Phase 3
  must retain that arrangement.
- The plan suggested a YAML sidecar “e.g.”. This brief chooses **JSON**. JSON
  has unambiguous scalar types, a strict no-NaN mode, deterministic canonical
  serialization, and can be consumed without PyYAML. This is a justified
  clarification, not a change in architecture.
- The plan implied one `tckdb_evidence.yml` next to `output.yml` but did not
  address two-file crash consistency. This brief adds a shared `document_id`
  descriptor and specifies write order so a mixed-generation pair is detected
  and ignored rather than consumed.
- Because adding the descriptor changes `output.yml`, new ARC output is
  version **1.1**. The standalone reader supports both `1.0` (old, no
  descriptor) and `1.1` (descriptor present). This follows the plan's stated
  version-bump rule.

## 3. Explicit non-goals

- No Zeus, `n170`, PBS, scheduler submission, remote deployment, environment
  installation, benchmark rerun, or live TCKDB upload.
- No network calls in tests; use `upload=False` and mocked clients only.
- No Phase 4 deletion or removal of the dual-path imports in `ARC.py`.
- No change to TCKDB backend or `tckdb-schemas`; the sidecar is an inter-package
  producer contract, not a public TCKDB upload payload.
- No general parser refactor and no move of ARC parsers into this repository.
- No NEB parser lift. Existing NEB single-point behavior is unchanged.
- No sidecars for opt/SP/thermo/kinetics or raw artifact embedding.
- No attempt to make two filesystem renames transactionally atomic. The
  generation-ID check supplies safe detect-and-fallback semantics.
- No `AGENTS.md` change. The existing repository instructions already cover
  environment, testing, minimal diffs, and branch discipline; Phase 3 adds no
  durable agent rule.

A separately approved integration smoke test may be useful after local Phase 3
stabilizes and before Phase 4. It is deliberately not part of this brief.

## 4. Ownership and dependency boundary

ARC owns production because it owns the artifacts and parsers. It must not
import `tckdb_arc`, `tckdb_client`, or `tckdb_schemas` to produce evidence.

`tckdb_arc` owns validation, selection, and conversion from evidence to TCKDB
payload dictionaries. Evidence data should be close to the parsed scientific
facts, not copied TCKDB request objects. In particular:

- ARC records native units and source indices;
- `tckdb_arc` continues to calculate TCKDB relative-energy fields and compose
  Hessian/IRC/path-search payloads;
- evidence provenance is not forwarded as arbitrary payload keys; and
- a change to the TCKDB wire schema does not require ARC to import that schema.

## 5. Files and symbols

### ARC repository

Add:

- `arc/tckdb_evidence.py`
  - `EVIDENCE_SCHEMA_NAME = "arc-tckdb-evidence"`
  - `EVIDENCE_SCHEMA_VERSION = "1.0"`
  - parser-version constants listed below
  - `build_tckdb_evidence(*, output_doc, project_directory, document_id)`
  - private per-kind builders and JSON-native validation helpers
  - `write_tckdb_evidence_atomic(*, evidence_doc, output_directory)`
- `arc/tckdb_evidence_test.py`
- small deterministic ESS/stringfile fixtures only when an existing ARC
  fixture cannot exercise a required shape

Modify:

- `arc/output.py::write_output_yml`
- `arc/output_test.py::TestWriteOutputYml`

Do not put this producer under `arc/tckdb/`: it must survive Phase 4.

### `tckdb-adapters` repository

Add:

- `tckdb_arc/tckdb_arc/evidence.py`
  - constants for supported output/evidence versions
  - `EvidenceIssue` and `EvidenceLookup` dataclasses (or equivalently small
    immutable value types)
  - `EvidenceStore` with lazy `for_output_doc(...)` loading and per-record
    lookup
  - strict document/envelope/value validators
- `tckdb_arc/tests/test_evidence.py`
- `tckdb_arc/tests/fixtures/golden/phase3_output.yml`
- `tckdb_arc/tests/fixtures/golden/tckdb_evidence.json`
- only the minimal source logs/stringfile/node outputs needed by fallback
  parity tests; do not commit a complete ARC run directory

Modify:

- `tckdb_arc/tckdb_arc/adapter.py`
  - instantiate/cache `EvidenceStore` on `TCKDBAdapter`
  - pass `output_doc` into `_build_freq_hessian_payload`
  - pass `output_doc` into `_parse_irc_trajectories`
  - pass a normalized GSM evidence value into
    `_build_path_search_result_payload`
- `tckdb_arc/tckdb_arc/sweep.py::run_upload_sweep`
  - validate the output schema before dispatch
- `tckdb_arc/tests/test_adapter.py`
- `tckdb_arc/tests/test_golden_corpus.py`
- package-data configuration only if the chosen fixture/schema arrangement
  requires it; tests themselves are not wheel data

Do not modify `_arc_optional.py` except documentation or a test seam if needed.
Its wrappers remain the old-run fallback.

## 6. File names, descriptor, and two-file write protocol

The canonical paths are:

```text
<project>/output/output.yml
<project>/output/tckdb_evidence.json
```

New `output.yml` documents use:

```yaml
schema_version: '1.1'
tckdb_evidence:
  path: tckdb_evidence.json
  schema_name: arc-tckdb-evidence
  schema_version: '1.0'
  document_id: 7b35f6d468d94cf39f5a8db7f2de3f11
```

Rules:

- `path` is a basename relative to the directory containing `output.yml`.
  Reject absolute paths, `..`, separators, symlinks that resolve outside that
  directory, and any name other than `tckdb_evidence.json` in v1.
- `document_id` is a lowercase 32-character UUID4 hex string generated once
  per `write_output_yml` call. It appears identically in both files.
- The evidence descriptor is added only if a complete evidence document was
  built and its atomic write succeeded.
- Evidence production is best-effort at the **entry** level, but creation of
  the document and descriptor is not silently half-done.

Write protocol:

1. Build the entire `output_doc` in memory with `schema_version: '1.1'`.
2. Generate one `document_id`.
3. Build all evidence entries independently; parser failure produces an
   `unavailable` envelope, not an exception from `write_output_yml`.
4. Serialize evidence with UTF-8, indent 2, `sort_keys=True`, trailing newline,
   and `allow_nan=False` to a temporary file in `output/`.
5. Flush and `os.fsync` the evidence temporary file, then
   `os.replace(temp, tckdb_evidence.json)`.
6. Add the descriptor to `output_doc` and atomically write `output.yml` using
   its existing temporary-file path.
7. If building or writing the evidence **document itself** fails, log one
   warning, omit the descriptor, and still write a valid `output.yml` 1.1.
8. Always remove owned temporary files on exceptions.

Writing evidence first is intentional. A crash between replacements can leave
an old `output.yml` beside new evidence; `document_id` mismatch makes the
consumer ignore it. Writing output first could advertise evidence that was
never completed. No mismatched evidence may be consumed.

Do not preserve a prior-generation sidecar when a new output is successfully
written without a descriptor. The unreferenced file is harmless and ignored;
deleting it is optional cleanup, not correctness. Never let cleanup failure
prevent output generation.

## 7. Evidence schema v1

Top-level JSON shape (all keys shown are required):

```json
{
  "schema_name": "arc-tckdb-evidence",
  "schema_version": "1.0",
  "document_id": "7b35f6d468d94cf39f5a8db7f2de3f11",
  "output_schema_version": "1.1",
  "producer": {
    "name": "ARC",
    "version": "1.1.0",
    "git_commit": "0123456789abcdef"
  },
  "records": []
}
```

`producer.version` and `git_commit` must be copied from the already-built
`output_doc` (`arc_version`, `arc_git_commit`), not rediscovered. A missing git
commit is JSON `null`. Do not add wall-clock timestamps: `output.yml` already
has them, and excluding one makes golden output deterministic.

Each record is:

```json
{
  "record_kind": "species",
  "label": "CH4",
  "freq_hessian": { "status": "available", "value": {} }
}
```

or:

```json
{
  "record_kind": "transition_state",
  "label": "TS0",
  "freq_hessian": { "status": "available", "value": {} },
  "irc": { "status": "available", "value": {} },
  "gsm": { "status": "available", "value": {} }
}
```

Record rules:

- `record_kind` is exactly `species` or `transition_state`.
- `(record_kind, label)` is unique and must resolve to exactly one output.yml
  record. Labels are case-sensitive; never fall back to `original_label`.
- Evidence-kind keys are omitted when not applicable (for example, no
  `freq_log`, no `irc_logs`, or chosen method is not GSM).
- If a source was applicable and ARC attempted it, include an envelope.
- Unknown keys are rejected by the consumer for v1. This keeps accidental
  misspellings from looking like partial data.

Every evidence-kind envelope is one of:

```json
{ "status": "available", "value": { } }
```

```json
{
  "status": "unavailable",
  "reason": "parse_failed",
  "source_paths": ["calcs/TSs/TS0/irc_a/output.log"]
}
```

Allowed reasons are `missing_source`, `unsupported_source`, `parse_failed`,
and `empty_result`. `source_paths` are optional diagnostic run-relative paths.
Do not serialize exception messages or tracebacks into the scientific
contract. Non-finite floats are invalid everywhere.

### 7.1 Frequency Hessian value

```json
{
  "source_log": "calcs/Species/CH4/freq/output.log",
  "geometry_xyz_text": "5\nCH4\nC ...\n",
  "atom_count": 5,
  "matrix_dimension": 15,
  "packing": "lower_triangle_row_major_including_diagonal",
  "units": "hartree_per_bohr_squared",
  "source": "parsed_log",
  "parser_version": "arc-hessian-1",
  "lower_triangle": [0.1, 0.0, 0.2]
}
```

Validation and semantics:

- `source` is `parsed_log` for Gaussian or `parsed_hess` for Orca.
- `geometry_xyz_text` is canonical XYZ with atom-count and comment lines, in
  the same atom order and Angstrom coordinates as the frequency input
  geometry (`record['xyz']` after adapter normalization today).
- `matrix_dimension == 3 * atom_count`.
- `len(lower_triangle) == d * (d + 1) / 2`.
- Packing includes the diagonal and is row-major.
- Values are native hartree/bohr². No conversion occurs in ARC or adapter.
- Monatomic/unsupported/malformed/no-block cases are `unavailable`, matching
  current parser behavior.

The consumer translates `lower_triangle` to the existing TCKDB key
`lower_triangle_hartree_bohr2`; the resulting Hessian payload must be exactly
equal to fallback output.

### 7.2 IRC value

```json
{
  "parser_version": "arc-irc-path-1",
  "trajectories": [
    {
      "source_log": "calcs/TSs/TS0/irc_1/output.log",
      "declared_direction": "forward",
      "points": [
        {
          "source_point_index": 1,
          "direction": "forward",
          "geometry_xyz_text": "3\nTS0 IRC point 1\n...\n",
          "electronic_energy_hartree": -115.2,
          "reaction_coordinate_sqrt_amu_bohr": 0.42,
          "max_gradient_hartree_per_bohr": 0.001,
          "rms_gradient_hartree_per_bohr": 0.0004
        }
      ]
    }
  ]
}
```

Rules:

- Trajectory order is `irc_logs` order; point order is parser/file order.
- `declared_direction` uses the current precedence: paired
  `irc_log_directions`, then filename heuristic, then null.
- `direction` is the rich parser's per-point direction, falling back to
  `declared_direction`; it is `forward`, `reverse`, or null.
- `source_point_index` is the ESS point number when rich data provides it;
  for geometry-only fallback it is the zero-based index within that source
  trajectory. It is provenance, not the final TCKDB global `point_index`.
- Every point must contain at least a valid geometry. Optional scalar fields
  are omitted, never explicitly fabricated as zero.
- Energies are hartree. Gaussian reaction coordinate is sqrt(amu)·bohr.
  Gradients retain ARC parser units, hartree/bohr.
- A rich parse is preferred per log; `parse_irc_traj` remains ARC producer's
  geometry-only fallback. If one log fails and another succeeds, emit the
  successful trajectory and record the failed source in optional
  `omitted_source_paths`; the envelope remains `available`.
- The TS marker and zero-energy reference are **not** stored here. They are
  already available from `output.yml` and remain composed by
  `_resolve_irc_zero_energy_reference` / `_build_irc_result_payload`.

The consumer allocates final point indices globally in the same order as the
fallback builder and appends the TS marker exactly as it does today.

### 7.3 GSM value

```json
{
  "source_stringfile": "calcs/TSs/TS0/xtb_gsm/stringfile.xyz0000",
  "parser_version": "arc-gsm-stringfile-1",
  "method": "gsm",
  "selected_source_point_index": 8,
  "points": [
    {
      "source_point_index": 0,
      "node_label": null,
      "geometry_xyz_text": "6\nGSM point 0\n...\n",
      "path_coordinate_angstrom": 0.0
    },
    {
      "source_point_index": 1,
      "node_label": 1,
      "geometry_xyz_text": "6\nGSM point 1\n...\n",
      "path_coordinate_angstrom": 0.12,
      "electronic_energy_hartree": -22.4,
      "stringfile_relative_energy_kcal_mol": 1.2,
      "max_gradient_hartree_per_bohr": 0.03,
      "rms_gradient_hartree_per_bohr": 0.01
    }
  ]
}
```

Rules:

- Frames are in stringfile order and `source_point_index` is zero-based.
- `selected_source_point_index` uses the current ARC/xTB-GSM rule
  `int((len(frames) - 1) / 2) + 1`; it must identify an emitted point.
- `path_coordinate_angstrom` is cumulative Kabsch-aligned displacement in
  Angstrom. All points either have it, beginning at 0 and monotonically
  nondecreasing, or all omit it if alignment fails.
- `node_label` records the preserved file label. Current label `N` maps to
  frame `N`; endpoint frame 0 normally has no node output and uses null.
- Node-output priority remains `.energy`/`.gradient` first, `.xtbout` second.
- `electronic_energy_hartree` is absolute. Gradient values retain the current
  parser's hartree/bohr units.
- `stringfile_relative_energy_kcal_mol` is included only when the comment
  column parsed. An all-flat column within `_GSM_STRINGFILE_ENERGY_EPS` is
  preserved as parsed evidence but remains the “no energy emitted” sentinel;
  the consumer must not turn it into a zero-energy profile.
- Atom count, symbols, and order must be constant across frames.

The consumer retains current precedence: absolute node energies win and are
referenced to their minimum; otherwise a non-flat stringfile relative-energy
column is converted kcal/mol → kJ/mol. It retains the selected-point and
climbing-image flags. This conversion must produce the same final
`PathSearchResultPayload` dictionary as fallback.

## 8. Producer behavior in ARC

`build_tckdb_evidence` receives the completed in-memory `output_doc`; it must
not walk live scheduler or species objects. This keeps replay semantics tied
to the same durable record the adapter sees.

For each output species and transition-state record:

1. If `freq_log` exists, attempt Hessian evidence.
2. For transition states with `irc_logs`, parse each log rich-first,
   geometry-only second.
3. For a transition state where the existing
   `_resolve_ts_guess_path_search_for_record(record)` logic resolves to `gsm`
   and `gsm_log` exists, parse the stringfile and node-output directory.
4. Catch failures per evidence kind and emit an `unavailable` envelope.
5. Continue processing other records and kinds.

Reuse existing ARC implementations:

- `determine_ess` and `ess_factory` for Hessian dispatch;
- ESS adapter `parse_cartesian_hessian_lower_triangle`;
- `parse_irc_path` / `parse_irc_traj`;
- `parse_trajectory` / `parse_gsm_stringfile_energies`;
- `xyz_to_str` and `kabsch`;
- lift the standalone `_read_gsm_node_outputs` parsing rules into ARC without
  importing `tckdb_arc`. Keep behavior and tests equivalent.

Producer logging:

- one DEBUG line per unavailable evidence item with kind, record label, reason,
  and source path;
- one INFO summary after successful write: available/unavailable counts by
  kind and path;
- one WARNING only for document-level build/write failure;
- never fail the main `output.yml` write solely because optional evidence
  parsing failed.

## 9. Consumer precedence and validation

`EvidenceStore` is lazy because many adapter modes never touch these paths.
The first evidence lookup validates and caches either a usable indexed
document or a document-level issue. Repeated species/reaction builds must not
re-read the file or repeat warnings.

Read protocol:

1. Resolve the descriptor path against the `output.yml` directory and apply
   the path restrictions before opening it.
2. Open the sidecar once in binary mode and read that file descriptor to EOF;
   an atomic producer replacement after `open` therefore cannot splice two
   generations into one read.
3. Reject a file larger than 256 MiB before JSON decoding. This is a generous
   ceiling for packed Hessians while bounding accidental/untrusted input.
4. Decode strictly as UTF-8 and JSON; reject duplicate JSON object keys (use an
   `object_pairs_hook`, not the default last-key-wins behavior).
5. Validate the descriptor/document IDs and the complete top-level contract
   before indexing records.
6. Cache the accepted store or failure result for the adapter lifetime.

The reader must not wait, poll, or retry a mismatched generation. A later CLI
invocation or adapter instance will observe the completed pair; the current
sweep safely uses fallback.

Before sweep dispatch:

- require `output_doc` to be a mapping;
- support output schema `1.0` and `1.1`;
- fail the sweep clearly for any other version rather than guessing;
- `1.0` has no descriptor and uses fallback behavior;
- a `1.1` document may lack a descriptor when evidence production failed; it
  also uses fallback behavior.

Lookup precedence for each `(record_kind, label, evidence_kind)`:

1. **Valid `available` value:** use it; do not touch `_arc_optional` or source
   files for that kind.
2. **Valid `unavailable` envelope:** treat producer absence as authoritative;
   omit that optional structured result and do **not** reparse. The producer
   already attempted the current artifact.
3. **No descriptor/file, document-ID mismatch, unsupported evidence version,
   malformed document, missing record/kind, or invalid individual value:**
   warn at most once per issue key and use the existing ARC reparse fallback.

The distinction in steps 2 and 3 is important: “attempted and unavailable” is
not the same as an old/partial sidecar. It avoids environment-dependent results
for new runs while preserving compatibility for incomplete and old runs.

Validation must reject:

- wrong schema name/version or descriptor disagreement;
- path traversal or pair mismatch;
- duplicate record keys;
- sidecar records absent from the corresponding output section;
- booleans where integers/floats are expected;
- NaN/infinity;
- invalid enums/directions/reasons;
- negative or duplicate indices;
- malformed XYZ, inconsistent atom count/order;
- Hessian length/dimension mismatch;
- selected GSM index not present;
- mixed/descending GSM path-coordinate series; and
- unknown v1 keys.

Document-level invalidity rejects the whole document. A malformed individual
record/envelope rejects only that lookup so another record can still use valid
evidence. Never merge a partially valid value with fallback parsing: use one
complete value or fallback for that evidence kind.

Consumer logging:

- INFO once when a sidecar is accepted, including schema version and counts;
- WARNING once for document-level invalidity/mismatch and fallback;
- WARNING once per malformed entry used by a lookup;
- DEBUG for ordinary absence on old output 1.0 and for authoritative
  `unavailable` values;
- no warning per species for the same cached document failure.

## 10. Adapter integration details

### Hessian

Change `_build_freq_hessian_payload` to accept `output_doc`. Resolve evidence by
record kind (`transition_state` when `species_record['is_ts']` else `species`)
and exact label. Convert a valid evidence value to the current payload shape.
Only if lookup says `fallback` execute the existing log resolution and
`_arc_optional.determine_ess`/`ess_factory` code.

### IRC

Change `_parse_irc_trajectories` to accept `output_doc`. Normalize valid IRC
evidence into the same internal trajectory abstraction consumed by
`_build_irc_result_payload`. Extend that abstraction to accept canonical
`geometry_xyz_text` directly; fallback parser output may be normalized into
the same form first. This gives one payload-composition path, preventing
sidecar/fallback drift.

Do not put final global indices, the TS marker, or relative-energy conversion
in two separate branches.

### GSM

Resolve GSM evidence in `_build_ts_block`, then pass it to
`_build_path_search_result_payload`. Refactor that helper into:

1. evidence/fallback normalization to one list of parser-neutral points; and
2. the existing TCKDB path-search payload composition.

Valid sidecar evidence must avoid calls to `parse_trajectory`,
`parse_gsm_stringfile_energies`, and `kabsch`, and must not require the source
stringfile or node-output directory to remain on disk. NEB and non-GSM
single-point fallback remain unchanged.

## 11. Failure semantics table

| Condition | Consumer action | ARC required? |
|---|---|---|
| output.yml 1.0, no descriptor | existing fallback | only for full parser-coupled data |
| output.yml 1.1, valid matching sidecar/value | sidecar primary | no |
| valid `unavailable` envelope | omit that result, no reparse | no |
| descriptor absent | fallback | as before |
| file missing/unreadable | warn once, fallback | as before |
| document ID mismatch | reject whole sidecar, fallback | as before |
| unsupported sidecar version | reject whole sidecar, fallback | as before |
| malformed JSON/root/provenance | reject whole sidecar, fallback | as before |
| one invalid record/value | other entries remain usable; affected kind falls back | only for affected kind |
| kind absent from otherwise valid record | fallback (partial sidecar compatibility) | only for affected kind |
| valid sidecar, original logs deleted | build complete result from sidecar | no |
| no ARC and fallback unavailable | preserve Phase 1 graceful omission/single-point behavior | no crash |

## 12. Required tests

All tests are offline.

### ARC producer tests

In `arc/tckdb_evidence_test.py` and `arc/output_test.py`, cover:

1. exact top-level v1 shape, deterministic JSON formatting, and no timestamp;
2. descriptor and sidecar share one document ID;
3. output schema is 1.1 when the new writer is used;
4. evidence is replaced before output (mock `os.replace` call order);
5. temporary files are removed on both success and failure;
6. document-level evidence failure still writes output without descriptor;
7. one parser failure emits `unavailable` while other evidence survives;
8. Gaussian Hessian available shape and exact triangle;
9. Orca Hessian source/shape;
10. Hessian dimension/triangle rejection and monatomic unavailable case;
11. rich Gaussian IRC with directions, energy, reaction coordinate, gradients,
    and geometry;
12. geometry-only IRC fallback;
13. one failed plus one successful IRC source yields available partial value
    with `omitted_source_paths`;
14. GSM frames, selected index, Kabsch cumulative coordinates, node-label
    mapping, `.energy` priority, and `.xtbout` fallback;
15. all-zero GSM comment energies preserved as evidence, not interpreted;
16. non-finite values cannot be serialized;
17. paths are run-relative and no server/job/credential fields leak in.

Use `arc_env` per ARC's `AGENTS.md`.

### Standalone evidence-loader tests

In `tests/test_evidence.py`, cover:

1. valid descriptor/document acceptance and indexed lookup;
2. lazy single read and cached single warning;
3. output 1.0 fallback;
4. output 1.1 with no descriptor fallback;
5. missing file, bad JSON, wrong root, wrong schema name, unsupported version,
   and document-ID mismatch;
6. absolute/traversal/wrong filename rejection;
7. duplicate/unknown record rejection;
8. each envelope status and reason;
9. invalid Hessian dimensions/packing/finite values;
10. invalid IRC direction/index/XYZ/units shape;
11. invalid GSM selected index, atom ordering, and path-coordinate monotonicity;
12. one bad value does not poison a different valid record;
13. valid `unavailable` is authoritative and distinct from `fallback`.

### Adapter parity and no-ARC tests

For each of Hessian, IRC, and GSM:

1. build once from source artifacts through mocked/current fallback parsers;
2. build once from the equivalent sidecar;
3. assert deep equality of the final calculation/result dictionary;
4. assert canonical emitted payload JSON is byte-identical;
5. validate against the pinned `tckdb_schemas` model;
6. poison every relevant `_arc_optional` wrapper to raise if called, delete or
   rename source artifacts, and prove valid sidecar construction still passes;
7. remove the sidecar and prove fallback is called;
8. use a valid `unavailable` envelope and prove fallback is not called;
9. corrupt only that value and prove fallback is called once.

Also exercise species and TS Hessians, two-direction IRC with TS marker, GSM
absolute-node-energy precedence, GSM non-flat relative-comment fallback, and
the all-zero comment sentinel.

### Golden contract

`tests/fixtures/golden/phase3_output.yml` and
`tckdb_evidence.json` are the first real frozen Phase 3 corpus, replacing the
Phase 1 TODO/synthetic-only limitation. They must be sanitized, small, and
scientifically representative. Include at least:

- one multi-atom freq Hessian;
- one TS with forward and reverse IRC points; and
- one GSM path with an energy-less endpoint plus absolute node energies.

`test_golden_corpus.py` must load files from disk (not helper-generated docs),
run computed-species, computed-reaction, and standalone-TS builds with
`upload=False`, validate all resulting payloads, enforce the existing
forbidden-key walker, and snapshot canonical JSON hashes. Document the source
ARC commit and sanitization in a fixture README.

The golden sidecar itself is the cross-repository contract. ARC producer tests
must generate an equivalent document (with deterministic patched document ID
and producer metadata) and compare the parsed dict to it. The standalone tests
consume that exact fixture. Do not maintain independently invented producer
and consumer examples.

## 13. Compatibility and acceptance criteria

Phase 3 is accepted only when all are true:

- New ARC output writes `output.yml` 1.1 and matching evidence 1.0 atomically
  under the protocol above.
- One parser failure cannot prevent `output.yml` or unrelated evidence.
- A base standalone installation with ARC unimportable builds complete
  Hessian, IRC, and GSM results from the golden sidecar after source artifacts
  are removed.
- Sidecar-primary and ARC-fallback final payload dictionaries and canonical
  payload files are identical for all three paths.
- Old output.yml 1.0 runs retain Phase 2 behavior.
- Valid authoritative-unavailable evidence never triggers environment-dependent
  reparsing.
- Unsupported/malformed/partial evidence never crashes a sweep and follows the
  table above with bounded logging.
- Current base standalone suite remains green; formerly ARC-gated tests that
  are representable by evidence gain a no-ARC leg rather than merely staying
  skipped.
- Focused ARC output/evidence tests and adjacent output writer tests pass in
  `arc_env`.
- No network, remote host, live upload, or Phase 4 deletion occurred.
- `rg` confirms ARC's producer imports none of `tckdb_arc`, `tckdb_client`, or
  `tckdb_schemas`, and standalone non-fallback modules import no `arc`.

Recommended local commands (adapt paths to the implementation checkout):

```bash
conda run -n arc_env python -m pytest arc/tckdb_evidence_test.py arc/output_test.py \
  -o addopts="" -p no:cacheprovider -q

python -m pytest tckdb_arc/tests/test_evidence.py \
  tckdb_arc/tests/test_adapter.py tckdb_arc/tests/test_golden_corpus.py -q

python -m pytest tckdb_arc/tests -q
```

The last standalone command must run in the Phase 1 development environment
with the pinned local/tagged `tckdb-client` and `tckdb-schemas`, and without
ARC for the base leg.

## 14. Implementation order

1. Freeze the sanitized golden source facts and expected v1 sidecar shape.
2. Implement ARC's pure per-kind builders and producer tests.
3. Integrate the two-file protocol into `write_output_yml` and test failure
   ordering.
4. Implement standalone version/descriptor/value validation and tests.
5. Add one normalized internal representation per evidence kind.
6. Route Hessian sidecar-first, then IRC, then GSM; keep fallback code intact.
7. Add parity, no-ARC, malformed, and golden-corpus tests.
8. Run focused and full local suites.
9. Append the Phase Log and ARC branch ledger only after implementation facts
   and exact test counts are known.

Do not begin by deleting or weakening fallback paths. The additive primary path
must prove parity first.

## 15. Repository and branch discipline

There are two repositories and therefore two reviewable commits.

### `tckdb-adapters`

- Start from current `origin/main` after confirming a clean worktree.
- Use a focused branch such as `feature_phase3_tckdb_evidence_consumer`.
- Keep consumer, fixtures, tests, and this repo's Phase Log together.
- Do not commit unrelated existing work.

### ARC

The plan's generic preference for a main-based ARC branch is not workable for
this phase as currently checked: `origin/main` has `arc/output.py`, but lacks
the TCKDB-expanded output contract, `arc/tckdb/`, and thousands of lines of the
parser/output provenance on which the three current paths rely. The actual
Phase 2 source was therefore based on `tckdb-imp`, as recorded in
`PHASE_LOG.md` and `arcbench/BRANCHES.md`.

- Base the Phase 3 producer branch on current `origin/tckdb-imp`, not
  `arcbench`, and name it `feature_tckdb_evidence_sidecar` (or equivalent).
- Keep `arc/tckdb_evidence.py` outside the soon-to-be-deleted `arc/tckdb/`.
- Record Base=`tckdb-imp`, purpose, source SHA, later integration SHAs, and
  Deployed=`n` in `~/code/arcbench/BRANCHES.md` when implementation is
  committed/integrated.
- If integrated, cherry-pick the focused producer commit to `arcbench` and
  mirror according to the existing ledger rules; do not merge the divergent
  branch wholesale and do not rebase pushed branches.
- Do not deploy as part of Phase 3.

The implementation agent must re-check branch tips and dirty worktrees before
editing; the SHAs in the Phase 2 log are historical context, not commands to
reset user work.

## 16. Phase Log and handoff requirements

Append a Phase 3 section to `docs/PHASE_LOG.md` only after code and tests exist.
It must record:

- status (`IN PROGRESS`, `DONE`, or `BLOCKED`);
- exact ARC and standalone branches/bases/commit SHAs;
- files and public/private symbols added or changed;
- final schema names/versions and any deviation from this brief;
- producer/consumer precedence and failure semantics actually implemented;
- golden fixture provenance and sanitization;
- exact focused/full test commands, pass/skip/fail counts, and whether ARC was
  importable for each leg;
- confirmation that no remote deployment, job, or live upload occurred;
- deferred work and explicit Phase 4 entry criteria.

Update `arcbench/BRANCHES.md` for actual ARC branch/integration events. Do not
create a benchmark error entry unless a real benchmark error prompted a fix;
Phase 3 feature work alone is not such an error.

## 17. Risks and rollback boundaries

1. **Two-file skew.** Mitigated by evidence-first replacement plus exact
   document-ID matching. Never consume a mismatch.
2. **Duplicated normalization logic.** Mitigated by one parser-neutral internal
   representation and final composition shared by sidecar and fallback.
3. **Unit drift.** Every numeric field has a unit in its key or a fixed unit
   enum/string; parity tests cover conversions.
4. **GSM index drift.** Preserve source index and node label separately; validate
   mapping and selected index instead of silently zipping unequal arrays.
5. **Partial scientific evidence.** Use per-kind envelopes and explicit omitted
   sources. Never splice half a sidecar value with half a reparse.
6. **Output schema compatibility.** Support 1.0 and 1.1 explicitly; reject future
   versions loudly.
7. **Golden fixture size/licensing/secrets.** Minimize and sanitize paths,
   usernames, credentials, server IDs, and irrelevant log text; retain enough
   values to prove chemistry/indexing.
8. **Producer coupling to a TCKDB branch.** The module's location outside
   `arc/tckdb/` and parser-neutral schema make later upstream re-homing possible,
   but the current source branch must honestly remain `tckdb-imp`.

Rollback is additive and repository-local:

- Reverting the standalone consumer commit restores ARC-reparse behavior;
  evidence files become ignored extra output.
- Reverting the ARC producer commit stops new evidence emission; standalone
  fallback continues to support old output.
- Either repository can roll back independently before Phase 4.
- Do not remove `_arc_optional.py`, the in-tree `arc/tckdb/`, or the Phase 2
  import fallback until the separately approved pre-Phase-4 integration check
  has succeeded.

## 18. No unresolved implementation blocker

The schema, lifecycle, precedence, compatibility, and repository topology are
specified sufficiently to implement Phase 3 locally. The only coordination
requirement is to land producer and consumer changes as separate, reviewable
commits and record their exact integration state. No external system is needed
to begin or complete the local implementation.
