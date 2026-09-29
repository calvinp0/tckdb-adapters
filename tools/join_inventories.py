#!/usr/bin/env python3
"""Join the Phase A contract inventories into a gap matrix.

Five agents inventory the ARC->TCKDB contract from five angles (see
``docs/contract/FIELD_KEY.md``). This script performs the join between them
mechanically, because the join is bookkeeping over thousands of rows and a
human or a model doing it by reading will silently drop rows -- and a dropped
row is indistinguishable, downstream, from a real gap. Manufacturing a gap is
the one failure this whole audit exists to avoid.

The most important output is therefore not the matrix but the *near-miss*
report: paths that almost joined. A path spelled ``conformers.geometry`` in
one inventory and ``conformers[].geometry`` in another does not raise -- it
just fails to match, and reports as ARC_ABSENT. Those are flagged loudly for
repair before the matrix is trusted. Since the 0.51 refresh the check also
compares paths with their list markers removed, which is what catches a list
of models keyed as a leaf (``scheme.atom_params`` versus
``scheme.atom_params[]``).

What changed for the 0.51 refresh (tckdb-schemas 0.51.0, five roots):

* A4 now enumerates every route the adapter emits, one row per path, from a
  per-route catalog cross-checked against built payloads. So the adapter side
  of a row is *observed at its own path*. Route adjudication only ever borrows
  ARC-side supply (A2/A3) from a sibling route; it no longer infers that the
  adapter emits a route it has no row for.
* Route adjudication also runs for rows the adapter maps but no supply
  inventory keys at that route (the ``reaction_upload.species[]`` mirror and
  the reaction bundle's flattened ``freq_*``/``opt_*``/``sp_*`` scalars), not
  only for rows nobody supplies.
* ``ROUTE_ADJUDICATION.yml`` gained two row kinds besides per-concept
  rulings: ``flattened_alias`` (relates ``freq_n_imag`` to
  ``freq_result.n_imag``) and ``route_applicability`` (pattern rules: demand
  that is vacuous at a route, or gated on something ARC does not produce).
* A1 container, root and union-variant rows get the ``CONTAINER`` verdict with
  a roll-up of their descendants, except that breaking drift on a container
  whose descendants the adapter maps is ``BROKEN``.
* A1 ``row_kind: workflow_check`` rows are attached to their path as
  ``checks``.

Usage:
    python tools/join_inventories.py [--contract-dir docs/contract]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.exit("PyYAML is required: pip install pyyaml")


# Inventory file -> the agent that produces it. Missing files are reported
# rather than fatal, so the matrix can be regenerated as agents land.
INVENTORIES = {
    "A1": "TCKDB_DEMAND.yml",
    "A2": "ARC_SUPPLY_EXPORTED.yml",
    "A3": "ARC_SUPPLY_LATENT.yml",
    "A4": "ADAPTER_MAPPING.yml",
    "A5": "SCHEMA_DRIFT.yml",
}

VERDICTS = (
    "BROKEN",
    "ADAPTER_GAP",
    "ARC_LATENT",
    "SOURCE_UNCONFIRMED",
    "ROUTE_UNRESOLVED",
    "ARC_ABSENT",
    "WIRED",
    "NOT_APPLICABLE",
    "CONTAINER",
    "SURPLUS",
    "ORPHAN_MAPPING",
)

# Rows that carry no field-level gap of their own. ``CONTAINER`` is a nested
# model, root or union variant whose supply lives on its leaves (A1 emits one
# row per container; A2/A3/A4 key leaves only), so its verdict is a roll-up,
# not a gap. ``NOT_APPLICABLE`` is demand no producer can fill at that route
# (an IRC result on a minimum's calculation) -- ruled in
# ROUTE_ADJUDICATION.yml, never inferred.
NON_GAP_VERDICTS = {"WIRED", "SOURCE_UNCONFIRMED", "NOT_APPLICABLE", "CONTAINER"}

# TCKDB reuses sub-models (level_of_theory, software_release, geometry) under
# many parents, and the demand inventory enumerates every route to them. The
# supply inventories key each concept at fewer routes. A demand path with no
# supply at its own route, but whose sub-model tail is supplied elsewhere, is
# therefore UNDECIDED rather than absent: the supply may generalise across
# routes, or the two instances may be scientifically different (a
# correction's frequency-scale level of theory is not a calculation's level of
# theory). Asserting either way from path shape alone would manufacture a
# verdict, so these are surfaced for adjudication.
#
# Two segments minimum: a bare leaf like `note` or `version` is common enough
# that matching on it alone would relate unrelated fields.
MIN_TAIL_SEGMENTS = 2
MAX_TAIL_SEGMENTS = 5

# Near-miss detection. Two paths this similar that did not join are far more
# likely to be a spelling slip than two genuinely distinct fields.
NEAR_MISS_RATIO = 0.93


def normalise(path: str) -> str:
    """Reduce a path to its join key.

    Collapses the differences that are notational rather than semantic --
    whitespace, list-index noise, trailing dots -- while preserving the
    field names themselves, which are the actual identity of a row.
    """
    p = path.strip()
    p = re.sub(r"\[\s*\d+\s*\]", "[]", p)   # species[0] -> species[]
    p = re.sub(r"\.(\d+)(?=\.|$)", "[]", p)  # species.0.x -> species[].x
    p = re.sub(r"\s+", "", p)
    return p.rstrip(".")


def strip_lists(path: str) -> str:
    return path.replace("[]", "")


def strip_markers(path: str) -> str:
    """Remove list markers and union-variant braces, for near-miss checks."""
    return re.sub(r"\{[^}]*\}", "", strip_lists(path))


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or []
    if not isinstance(data, list):
        raise SystemExit(f"{path.name}: expected a top-level list, got {type(data).__name__}")
    rows = [r for r in data if isinstance(r, dict) and r.get("path")]
    if len(rows) != len(data):
        print(f"  ! {path.name}: skipped {len(data) - len(rows)} malformed row(s)", file=sys.stderr)
    return rows


def index_by_path(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[normalise(str(r["path"]))].append(r)
    return out


def truthy(value: Any) -> bool:
    """Interpret a YAML cell that agents may have written loosely."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y"}
    return False


def unverified(row: dict[str, Any]) -> bool:
    return any(
        isinstance(v, str) and v.strip().upper().startswith("UNVERIFIED")
        for v in row.values()
    )


def is_ts_route(path: str) -> bool:
    return path.startswith("ts_upload.") or ".transition_state." in f"{path}."


def tail_index(
    *indexes: dict[str, list[dict[str, Any]]], demand: set[str] | None = None
) -> dict[str, list[str]]:
    """Map every sub-model tail of every supplied path back to that path.

    Only paths that are themselves TCKDB demand paths are indexed. An
    ``arc_only.*`` row or a mis-keyed supply row has no TCKDB home, and
    letting it act as a sibling can silently suppress a real gap
    (ROUTE_ADJUDICATION.md §2.5).
    """
    out: dict[str, list[str]] = defaultdict(list)
    for idx in indexes:
        for path in idx:
            if demand is not None and path not in demand:
                continue
            segments = path.split(".")
            for n in range(MIN_TAIL_SEGMENTS, min(MAX_TAIL_SEGMENTS, len(segments)) + 1):
                key = ".".join(segments[-n:])
                if path not in out[key]:
                    out[key].append(path)
    return out


@dataclass
class Adjudication:
    """Everything ROUTE_ADJUDICATION.yml rules, split by row kind."""

    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)
    aliases: dict[str, dict[str, Any]] = field(default_factory=dict)
    applicability: list[tuple[re.Pattern[str], dict[str, Any]]] = field(default_factory=list)
    mapping_review: list[tuple[re.Pattern[str], dict[str, Any]]] = field(default_factory=list)
    mirrors: list[tuple[re.Pattern[str], dict[str, Any]]] = field(default_factory=list)
    orphan_rulings: int = 0


def load_adjudication(contract_dir: Path) -> Adjudication:
    """Load the route rulings.

    The mechanical join can only observe that a sub-model tail supplied at
    one route is absent at another. Whether that supply *generalises* is a
    scientific judgement -- a correction's frequency-scale level of theory is
    not a calculation's level of theory, even though the path tail is
    identical -- so it is adjudicated separately and applied here.
    """
    out = Adjudication()
    path = contract_dir / "ROUTE_ADJUDICATION.yml"
    if not path.exists():
        return out
    rows = yaml.safe_load(path.read_text()) or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "concept")
        if kind == "flattened_alias":
            out.aliases[str(row["flat_field"])] = row
        elif kind == "route_applicability":
            out.applicability.append((re.compile(str(row["pattern"])), row))
        elif kind == "mapping_review":
            out.mapping_review.append((re.compile(str(row["pattern"])), row))
        elif kind == "route_mirror":
            out.mirrors.append((re.compile(str(row["pattern"])), row))
        elif row.get("concept"):
            verdict = str(row.get("verdict", "")).strip().upper()
            if verdict in {"A1_MISS", "NEVER_EXISTED"}:
                # Orphan-mapping rulings, not route rulings: they must not
                # shadow a shorter concept that does rule on a route.
                out.orphan_rulings += 1
                continue
            out.concepts[normalise(str(row["concept"]))] = row
    return out


def _listed(path: str, row: dict[str, Any], key: str) -> bool:
    """Is ``path`` named by the ruling's ``key`` list or ``key``_patterns?"""
    values = row.get(key) or []
    if isinstance(values, str):
        values = [values]
    if path in {normalise(str(v)) for v in values}:
        return True
    patterns = row.get(f"{key}_patterns") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(re.search(p, path) for p in patterns)


def apply_ruling(path: str, ruling: dict[str, Any]) -> str | None:
    """Return GENERALISES / DIFFERENT_INSTANCE / VACUOUS for one row, or None.

    ``PARTIAL`` rulings enumerate the demand paths that do NOT generalise
    (``exceptions``, ``exceptions_patterns``) and the paths where the demand is
    vacuous (``vacuous``, ``vacuous_patterns``); a path in neither is covered by
    the ruling's general half.
    """
    verdict = str(ruling.get("verdict", "")).strip().upper()
    if _listed(path, ruling, "vacuous"):
        return "VACUOUS"
    if verdict == "PARTIAL":
        return "DIFFERENT_INSTANCE" if _listed(path, ruling, "exceptions") else "GENERALISES"
    if verdict in {"GENERALISES", "DIFFERENT_INSTANCE", "VACUOUS"}:
        return verdict
    return None


def concept_ruling(path: str, adj: Adjudication) -> tuple[str, dict[str, Any]] | None:
    """The most specific per-concept ruling whose concept is a tail of ``path``.

    Looked up by the path's own tails, longest first, rather than by the tail
    it happens to share with whichever sibling was picked: the ruling is about
    the concept, and which sibling carries the supply is a separate choice.
    """
    segments = path.split(".")
    for n in range(len(segments), MIN_TAIL_SEGMENTS - 1, -1):
        concept = ".".join(segments[-n:])
        if concept in adj.concepts:
            return concept, adj.concepts[concept]
    return None


def best_sibling(
    path: str,
    candidates: list[str],
    inv: dict[str, dict[str, list[dict[str, Any]]]],
) -> str:
    """Prefer a sibling that carries ARC-side supply, then one of the same kind.

    A sibling the adapter maps but no supply inventory keys cannot lend supply,
    and a transition-state route is a poorer witness for a minimum than
    another minimum is.
    """
    def rank(c: str) -> tuple[int, int]:
        supply = 2 if c in inv["A2"] else 1 if c in inv["A3"] else 0
        return (supply, int(is_ts_route(c) == is_ts_route(path)))

    return max(candidates, key=rank)


def find_sibling(
    path: str,
    tails: dict[str, list[str]],
    inv: dict[str, dict[str, list[dict[str, Any]]]],
) -> str | None:
    """Longest sub-model tail of ``path`` supplied at another route."""
    segments = path.split(".")
    for n in range(min(MAX_TAIL_SEGMENTS, len(segments)), MIN_TAIL_SEGMENTS - 1, -1):
        candidates = [c for c in tails.get(".".join(segments[-n:]), []) if c != path]
        if candidates:
            return best_sibling(path, candidates, inv)
    return None


@dataclass
class MatrixRow:
    path: str
    verdict: str
    tier: str = "untiered"
    demand: dict[str, Any] | None = None
    exported: dict[str, Any] | None = None
    latent: dict[str, Any] | None = None
    adapter: dict[str, Any] | None = None
    drift: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    borrowed_from: str | None = None
    ruling: str | None = None
    rollup: dict[str, int] | None = None

    @property
    def cost(self) -> str:
        if self.verdict == "BROKEN":
            return "forced"
        if self.verdict == "ADAPTER_GAP":
            return "low"
        if self.verdict == "ARC_LATENT" and self.latent:
            return {"trivial": "low", "moderate": "medium", "hard": "high"}.get(
                str(self.latent.get("export_effort", "")).strip().lower(), "unknown"
            )
        return "n/a"


def load_tiers(contract_dir: Path) -> list[tuple[str, str]]:
    """Extract (tier_name, glob) pairs from PRIORITY_POLICY.md's YAML blocks.

    The policy is prose with embedded fenced blocks rather than a data file,
    because it is a document a human maintains and argues with. Parsing it here
    keeps the single source of truth in the document.
    """
    policy = contract_dir / "PRIORITY_POLICY.md"
    if not policy.exists():
        return []
    blocks = re.findall(r"```yaml\n(.*?)```", policy.read_text(), re.S)
    pairs: list[tuple[str, str]] = []
    for block in blocks:
        try:
            parsed = yaml.safe_load(block) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for tier, globs in parsed.items():
            if not str(tier).startswith("tier_") or not isinstance(globs, list):
                continue
            for g in globs:
                if isinstance(g, str) and g.strip():
                    pairs.append((str(tier), g.strip()))
    return pairs


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Compile a policy glob.

    Three wildcard forms, deliberately distinct:
      ``**.x``  matches ``x`` at any depth -- the same field reached by several
                routes carries the same value on each.
      ``x.*``   matches the whole subtree under ``x``, not merely one segment
                below it. ``atom_map.*`` has to cover
                ``atom_map.reactants[].atom_indices``.
      ``x.*.y`` an interior ``*`` matches exactly one segment.

    List markers and union-variant braces are stripped from both sides before
    matching: the policy is written about fields, and whether a field sits
    under a list is a detail of the path, not of its scientific value.
    """
    pattern = re.escape(strip_markers(glob))
    pattern = pattern.replace(r"\*\*\.", r"(?:[^.]+\.)*")  # **. -> any prefix
    if pattern.endswith(r"\.\*"):                          # trailing .* -> subtree
        pattern = pattern[: -len(r"\.\*")] + r"(?:\..+)?"
    pattern = pattern.replace(r"\*", r"[^.]+")             # interior * -> one segment
    return re.compile(pattern)


def _specificity(glob: str) -> tuple[int, int]:
    """Rank competing globs so the most specific one claims a path.

    A glob ending in a literal field name beats one ending in a wildcard, which
    is what keeps ``**.note`` (tier 4) from losing every ``note`` in the tree to
    a broad subtree rule like ``reaction_upload.transition_state.*`` (tier 1).
    Ties break on the number of literal segments, then on document order.
    """
    segments = strip_lists(glob).split(".")
    ends_literal = 0 if segments[-1] in {"*", "**"} else 1
    literal_segments = sum(1 for s in segments if s not in {"*", "**"})
    return (ends_literal, literal_segments)


def tier_for(path: str, tiers: list[tuple[str, str]]) -> str:
    """Assign a tier by most-specific match, not by document order.

    Document order cannot express "notes are tier 4 wherever they appear",
    because a broad tier-1 subtree rule would always reach them first.
    """
    target = strip_markers(path)
    best: tuple[tuple[int, int], str] | None = None
    for tier, glob in tiers:
        if _glob_to_regex(glob).fullmatch(target):
            score = _specificity(glob)
            if best is None or score > best[0]:
                best = (score, tier)
    return best[1] if best else "untiered"


def container_paths(paths: set[str]) -> set[str]:
    """Demand paths that are a strict path-prefix of another demand path."""
    prefixes: set[str] = set()
    for p in paths:
        for m in re.finditer(r"[.\[{]", p):
            prefixes.add(p[: m.start()])
    return {p for p in paths if p in prefixes}


def descendants(path: str, paths: list[str]) -> list[str]:
    return [q for q in paths if q != path and q.startswith(path) and q[len(path)] in ".[{"]


def build(contract_dir: Path) -> tuple[list[MatrixRow], dict[str, Any]]:
    inv: dict[str, dict[str, list[dict[str, Any]]]] = {}
    missing = []
    raw_counts: dict[str, int] = {}
    checks_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for agent, filename in INVENTORIES.items():
        rows = load(contract_dir / filename)
        raw_counts[agent] = len(rows)
        if not rows:
            missing.append(f"{agent} ({filename})")
        if agent == "A1":
            field_rows = [r for r in rows if r.get("row_kind", "field") == "field"]
            for r in rows:
                if r.get("row_kind") == "workflow_check":
                    checks_by_path[normalise(str(r["path"]))].append(r)
            rows = field_rows
        inv[agent] = index_by_path(rows)

    demand_paths = set(inv["A1"])
    containers = container_paths(demand_paths)
    tiers = load_tiers(contract_dir)
    adj = load_adjudication(contract_dir)
    tails = tail_index(inv["A2"], inv["A3"], inv["A4"], demand=demand_paths)
    matrix: list[MatrixRow] = []
    applied = Counter()

    for path, demand_rows in sorted(inv["A1"].items()):
        demand = demand_rows[0]
        if len(demand_rows) > 1:
            demand = dict(demand)
            demand["_duplicate_count"] = len(demand_rows)
        exported = inv["A2"].get(path, [None])[0]
        latent = inv["A3"].get(path, [None])[0]
        adapter = inv["A4"].get(path, [None])[0]
        drift = inv["A5"].get(path, [])
        breaking = [d for d in drift if truthy(d.get("breaking_for_producer"))]
        row = MatrixRow(
            path=path, verdict="", tier=tier_for(path, tiers), demand=demand,
            exported=exported, latent=latent, adapter=adapter, drift=drift,
            checks=[
                {k: c.get(k) for k in ("check", "code", "tier", "http_status", "on_violation", "source")}
                for c in checks_by_path.get(path, [])
            ],
        )

        if path in containers:
            row.verdict = "CONTAINER"
            if breaking and any(d in inv["A4"] for d in descendants(path, sorted(demand_paths))):
                row.verdict = "BROKEN"
                row.flags.append("BREAKING_ON_CONTAINER")
            matrix.append(row)
            continue

        borrowed_exported = borrowed_latent = None
        own_supply = exported is not None or latent is not None
        forced: str | None = None

        if not own_supply:
            # 1. Pattern rules: vacuous or gated demand at this route.
            rule = next(
                (r for pat, r in adj.applicability if pat.search(path)), None
            )
            if rule is not None and adapter is None:
                kind = str(rule.get("verdict", "")).upper()
                row.ruling = f"applicability:{rule.get('rule_id')}"
                applied[row.ruling] += 1
                forced = "NOT_APPLICABLE" if kind == "VACUOUS" else "ARC_ABSENT"
                row.flags.append(f"RULE:{rule.get('rule_id')}")
            else:
                # 2. Flattened reaction-calculation scalar -> nested result alias.
                leaf = path.rsplit(".", 1)[-1]
                alias = adj.aliases.get(leaf)
                ruling: str | None = None
                ruling_row: dict[str, Any] = {}
                sibling: str | None = None
                mirror = None
                for pat, mrow in adj.mirrors:
                    if pat.search(path) and not _listed(path, mrow, "exceptions"):
                        candidate = pat.sub(str(mrow["replacement"]), path, count=1)
                        if candidate in inv["A2"] or candidate in inv["A3"]:
                            mirror = (candidate, mrow)
                            break
                if mirror is not None:
                    # 2a. The same ARC record reached through a mirrored
                    # route (reaction_upload.species[] <-> species_upload,
                    # conformer_upload <-> species_upload): borrow directly.
                    sibling, ruling_row = mirror
                    ruling = "GENERALISES"
                    row.ruling = f"mirror:{ruling_row.get('rule_id')}"
                elif alias is not None:
                    nested = path.rsplit(".", 1)[0] + "." + str(alias["nested_tail"])
                    sibling = find_sibling(nested, tails, inv)
                    ruling = apply_ruling(path, alias)
                    ruling_row = alias
                    row.ruling = f"alias:{leaf}"
                else:
                    # 3. Per-concept route ruling.
                    sibling = find_sibling(path, tails, inv)
                    found = concept_ruling(path, adj) if sibling else None
                    if found:
                        concept, r = found
                        ruling = apply_ruling(path, r)
                        ruling_row = r
                        row.ruling = f"concept:{concept}"
                if sibling:
                    row.flags.append(f"SUPPLIED_AT:{sibling}")
                if ruling:
                    row.flags.append(f"ADJUDICATED:{ruling}")
                    applied[f"{row.ruling}->{ruling}"] += 1
                if ruling == "VACUOUS" and adapter is None:
                    forced = "NOT_APPLICABLE"
                elif (
                    ruling == "DIFFERENT_INSTANCE"
                    and adapter is None
                    and ruling_row.get("different_instance_verdict")
                ):
                    # The sibling is a different object, but the ruling
                    # records what IS true of this route (for example: ARC
                    # exports the datum inside another field, so the gap is
                    # the adapter's).
                    forced = str(ruling_row["different_instance_verdict"])
                elif ruling == "GENERALISES" and sibling:
                    borrowed_exported = inv["A2"].get(sibling, [None])[0]
                    borrowed_latent = inv["A3"].get(sibling, [None])[0]
                    row.borrowed_from = sibling
                    if borrowed_exported is None and borrowed_latent is None:
                        row.flags.append("SIBLING_SOURCE_UNCONFIRMED")
                elif ruling is None and sibling and adapter is None:
                    forced = "ROUTE_UNRESOLVED"

        eff_exported = exported or borrowed_exported
        eff_latent = latent or borrowed_latent
        if forced:
            verdict = forced
        elif adapter and breaking:
            verdict = "BROKEN"
        elif adapter and eff_exported:
            verdict = "WIRED"
        elif eff_exported and not adapter:
            verdict = "ADAPTER_GAP"
        elif eff_latent:
            verdict = "ARC_LATENT"
        elif adapter:
            # The adapter maps this, but no supply inventory has a row for it
            # at this route or at an adjudicated sibling. That is NOT a gap:
            # A2's remit stops at output.yml and the evidence sidecar, while
            # the adapter also reparses ESS logs and asserts constants.
            verdict = "SOURCE_UNCONFIRMED"
        elif row.borrowed_from and "SIBLING_SOURCE_UNCONFIRMED" in row.flags:
            # The concept generalises, the sibling's only supply is the
            # adapter itself, and the adapter does not emit this route. No
            # inventory confirms ARC exports the value, so this is an adapter
            # gap only if the adapter's own source at the sibling is real
            # ARC data -- marked, never counted as "ARC already exports it".
            verdict = "ADAPTER_GAP"
            row.flags.append("ARC_SUPPLY_UNCONFIRMED")
        else:
            verdict = "ARC_ABSENT"
        row.verdict = verdict

        # Silent-corruption sweep. These outrank every tier (PRIORITY_POLICY),
        # because they pass validation and write wrong science.
        haystack = " ".join(
            str(v)
            for src in (eff_exported, eff_latent, adapter, *drift)
            if src
            for v in src.values()
        ).lower()
        if re.search(r"0-based|1-based|zero-based|one-based|index base|rebase", haystack):
            row.flags.append("INDEX_BASE")
        # Naming a unit is not a risk -- converting between two is.
        if re.search(
            r"convert|multiply|divide by|×\s*\d|\*\s*\d+\.\d|"
            r"(kcal|hartree|cm\^-1|j/mol)[^.]{0,40}(->|→|\bto\b)[^.]{0,20}(kj|kcal|hartree)|"
            r"unit mismatch|differs? in unit|no unit|unitless",
            haystack,
        ):
            row.flags.append("UNIT_CONVERSION")
        if any(str(d.get("change")) == "semantics_changed" for d in drift):
            row.flags.append("SEMANTIC_SHIFT")
        if any(unverified(r) for r in (demand, exported, latent, adapter) if r):
            row.flags.append("UNVERIFIED")
        if row.verdict == "ARC_ABSENT" and eff_latent is None and eff_exported is None:
            row.flags.append("NO_ARC_EVIDENCE")
        if row.borrowed_from:
            row.flags.append(f"BORROWED_FROM:{row.borrowed_from}")
        # Phase B mapping review: the adapter emits the field, but from an ARC
        # key that A2 shows ARC does not write for this record (a dead or
        # stale mapping). Recorded per rule in ROUTE_ADJUDICATION.yml, never
        # inferred from string similarity -- an automatic key comparison was
        # tried and flagged mostly notation differences.
        review = next((r for pat, r in adj.mapping_review if pat.search(path)), None)
        when = set(review.get("when_verdicts") or ["WIRED", "SOURCE_UNCONFIRMED"]) if review else set()
        if review is not None and verdict in when:
            row.verdict = str(review["verdict_override"])
            row.flags.append(f"MAPPING_REVIEW:{review.get('rule_id')}")
            applied[f"mapping_review:{review.get('rule_id')}"] += 1
        if any(c.get("tier") == "block" for c in row.checks):
            row.flags.append("BLOCK_CHECK")
        matrix.append(row)

    # Roll-ups for containers: descendant leaf verdicts.
    by_path = {r.path: r for r in matrix}
    leaf_paths = sorted(p for p in demand_paths if p not in containers)
    for r in matrix:
        if r.path in containers:
            r.rollup = dict(Counter(by_path[d].verdict for d in descendants(r.path, leaf_paths)))

    # Rows in ARC/adapter inventories with no TCKDB demand counterpart.
    for agent, verdict in (("A2", "SURPLUS"), ("A3", "SURPLUS"), ("A4", "ORPHAN_MAPPING")):
        for path, rows in sorted(inv[agent].items()):
            if path in inv["A1"]:
                continue
            r = rows[0]
            if path.startswith("arc_only.") and verdict == "ORPHAN_MAPPING":
                continue
            matrix.append(
                MatrixRow(
                    path=path,
                    verdict="SURPLUS" if path.startswith("arc_only.") else verdict,
                    tier=tier_for(path, tiers),
                    exported=r if agent == "A2" else None,
                    latent=r if agent == "A3" else None,
                    adapter=r if agent == "A4" else None,
                    flags=["NO_TCKDB_HOME"] if path.startswith("arc_only.") else ["UNJOINED"],
                )
            )

    # A5 rows whose path is not an A1 path: removed fields, reported so a
    # stale drift row is never silently lost.
    drift_unjoined = sorted(p for p in inv["A5"] if p not in inv["A1"])

    near_misses = find_near_misses(inv)
    stats = {
        "missing_inventories": missing,
        "rulings": len(adj.concepts),
        "aliases": len(adj.aliases),
        "applicability_rules": len(adj.applicability),
        "applied": applied,
        "counts": {a: sum(len(v) for v in inv[a].values()) for a in INVENTORIES},
        "raw_counts": raw_counts,
        "checks": sum(len(v) for v in checks_by_path.values()),
        "near_misses": near_misses,
        "drift_unjoined": [(p, inv["A5"][p][0].get("change")) for p in drift_unjoined],
        "tier_patterns": len(tiers),
        "containers": len(containers),
    }
    return matrix, stats


def find_near_misses(inv: dict[str, dict[str, list]]) -> list[tuple[str, str, str, float]]:
    """Paths that nearly joined against A1 but did not.

    The highest-value output of this script. A near-miss is almost always a
    spelling slip that will otherwise be reported as a real gap. A path that
    matches a demand path once list markers and union braces are removed is
    reported with similarity 1.0 regardless of string distance.
    """
    demand = list(inv["A1"])
    stripped: dict[str, list[str]] = defaultdict(list)
    for d in demand:
        stripped[strip_markers(d)].append(d)
    out = []
    for agent in ("A2", "A3", "A4", "A5"):
        for path in inv[agent]:
            if path in inv["A1"] or path.startswith("arc_only."):
                continue
            if agent == "A5" and all(r.get("change") == "removed" for r in inv[agent][path]):
                # A removed field is expected to be absent from A1; it is
                # listed separately, not as a spelling slip.
                continue
            exact = stripped.get(strip_markers(path))
            if exact:
                out.append((agent, path, exact[0], 1.0))
                continue
            best, score = None, 0.0
            tail = strip_markers(path).rsplit(".", 1)[-1]
            for cand in demand:
                if strip_markers(cand).rsplit(".", 1)[-1] != tail:
                    continue
                s = SequenceMatcher(None, path, cand).ratio()
                if s > score:
                    best, score = cand, s
            if best and score >= NEAR_MISS_RATIO:
                out.append((agent, path, best, round(score, 3)))
    return sorted(out, key=lambda x: -x[3])


MEANING = {
    "WIRED": "an ARC export and an adapter mapping exist (at this route or an adjudicated sibling), no breaking drift; the key exists, the value is not proven",
    "BROKEN": "adapter emits it but a 0.51 rule now refuses what it sends",
    "ADAPTER_GAP": "TCKDB wants it at this route and the adapter does not emit it there; ARC exports it unless flagged `ARC_SUPPLY_UNCONFIRMED`",
    "ARC_LATENT": "ARC computes it internally but never exports it",
    "SOURCE_UNCONFIRMED": "adapter emits it, but not from an inventoried export (reparse, constant, or unexported key)",
    "ARC_ABSENT": "ARC cannot currently produce it",
    "ROUTE_UNRESOLVED": "supplied at another route; no ruling yet on whether it generalises here",
    "NOT_APPLICABLE": "no producer can fill it at this route (ruled vacuous)",
    "CONTAINER": "nested model / root / union variant; its leaves carry the verdict",
    "SURPLUS": "ARC offers it, TCKDB has no home for it",
    "ORPHAN_MAPPING": "adapter targets a field absent from TCKDB 0.51.0",
}


def render(matrix: list[MatrixRow], stats: dict[str, Any]) -> str:
    counts = Counter(r.verdict for r in matrix)
    flagged = [
        r for r in matrix
        if {"INDEX_BASE", "UNIT_CONVERSION", "SEMANTIC_SHIFT"} & set(r.flags)
        or any(f.startswith("MAPPING_REVIEW") for f in r.flags)
        and r.verdict not in {"CONTAINER", "SURPLUS", "NOT_APPLICABLE"}
    ]

    lines = [
        "# Gap matrix — ARC supply vs TCKDB demand (tckdb-schemas 0.51.0)",
        "",
        "Generated by `tools/join_inventories.py`. Do not hand-edit; edit the Phase A",
        "inventories or `ROUTE_ADJUDICATION.yml` and regenerate. Verdicts are defined in",
        "the summary table below; the route rulings behind `NOT_APPLICABLE`, borrowed",
        "supply and `MAPPING_REVIEW` are in `ROUTE_ADJUDICATION.md` §7. The ranked,",
        "owner-sorted reading of this matrix is `BRIDGE_ROADMAP.md`.",
        "",
        "## Verdict summary",
        "",
        "| Verdict | Rows | Meaning |",
        "|---|---:|---|",
    ]
    for v in VERDICTS:
        if counts.get(v):
            lines.append(f"| `{v}` | {counts[v]} | {MEANING.get(v, '')} |")
    unconfirmed = sum(1 for r in matrix if "ARC_SUPPLY_UNCONFIRMED" in r.flags)
    lines += [
        "",
        f"**Total rows:** {len(matrix)}",
        "",
        f"Of the `ADAPTER_GAP` rows, {counts.get('ADAPTER_GAP', 0) - unconfirmed} have an A2 export "
        f"(at their own path or at an adjudicated sibling) and {unconfirmed} carry "
        "`ARC_SUPPLY_UNCONFIRMED` (`arc_supply: unconfirmed` in the YAML): the concept generalises, "
        "but the sibling's only supply is the adapter's own mapping, so no inventory confirms that "
        "ARC exports the value. Do not read those as \"ARC already exports it\".",
        "",
        "**What `WIRED` proves.** A `WIRED` row means an A2 export row and an A4 mapping row exist for "
        "the field, at its own path or through a route ruling (concept, mirror or alias; see "
        "`borrowed_from`). It proves the ARC key exists and the adapter emits the field. It does "
        "**not** prove the value is right. For example "
        "`ts_upload.additional_calculations[].level_of_theory.*` is `WIRED`, yet the IRC and "
        "TS-guess calculations on that route are labelled with the opt level (BRIDGE_ROADMAP.md "
        "top-10 item 3). Value correctness is judged in BRIDGE_ROADMAP.md, not here.",
        "",
    ]

    roots = sorted({r.path.split(".")[0] for r in matrix})
    present = [v for v in VERDICTS if counts.get(v)]
    lines += [
        "### By root",
        "",
        "| Root | " + " | ".join(f"`{v}`" for v in present) + " |",
        "|---|" + "---:|" * len(present),
    ]
    per_root = Counter((r.path.split(".")[0], r.verdict) for r in matrix)
    for root in roots:
        lines.append(f"| `{root}` | " + " | ".join(str(per_root.get((root, v), 0) or "") for v in present) + " |")
    lines += [
        "",
        f"Inputs: A1 {stats['raw_counts']['A1']} rows ({stats['counts']['A1']} field rows, "
        f"{stats['checks']} workflow checks), A2 {stats['counts']['A2']}, A3 {stats['counts']['A3']}, "
        f"A4 {stats['counts']['A4']}, A5 {stats['counts']['A5']}. "
        f"{stats['containers']} demand rows are containers. Route adjudication: "
        f"{stats['rulings']} concept rulings, {stats['aliases']} flattened aliases, "
        f"{stats['applicability_rules']} applicability rules.",
        "",
    ]

    if stats["missing_inventories"]:
        lines += [
            "> [!WARNING]",
            "> Missing inventories: " + ", ".join(stats["missing_inventories"]) + ".",
            "> The matrix below is INCOMPLETE and its verdicts are not yet trustworthy.",
            "",
        ]

    lines += ["## Near-miss paths", ""]
    if stats["near_misses"]:
        lines += [
            "These paths nearly matched a TCKDB demand path but did not join. Each is",
            "most likely a spelling slip, and each is currently being reported as a gap",
            "that may not exist.",
            "",
            "| Agent | Path as written | Nearest demand path | Similarity |",
            "|---|---|---|---:|",
        ]
        for agent, path, cand, score in stats["near_misses"]:
            lines.append(f"| {agent} | `{path}` | `{cand}` | {score} |")
    else:
        lines.append("None. Every A2/A3/A4 path that is not `arc_only.*` joins an A1 path.")
    lines.append("")
    if stats["drift_unjoined"]:
        lines += [
            "A5 rows with no A1 path (expected only for fields removed since 0.22):",
            "",
        ]
        for p, change in stats["drift_unjoined"]:
            lines.append(f"- `{p}` ({change})")
        lines.append("")

    broken = [r for r in matrix if r.verdict == "BROKEN"]
    if broken:
        lines += [
            "## Producer-breaking",
            "",
            "| Path | Drift | Flags |",
            "|---|---|---|",
        ]
        for r in broken:
            to = "; ".join(str(d.get("to", ""))[:160] for d in r.drift if truthy(d.get("breaking_for_producer")))
            lines.append(f"| `{r.path}` | {to} | {', '.join(r.flags)} |")
        lines.append("")

    if flagged:
        lines += [
            "## Silent-corruption risks",
            "",
            "Ranked above every value tier: these pass validation and can write wrong",
            "science. Unit conversions, atom-index base changes, fields whose meaning",
            "changed under an unchanged name, and adapter mappings Phase B reviewed and",
            "found reading an ARC key ARC does not write (`MAPPING_REVIEW`). A unit or",
            "index flag is a prompt to check the row, not a finding by itself.",
            "",
            "| Path | Verdict | Flags |",
            "|---|---|---|",
        ]
        for r in sorted(flagged, key=lambda r: r.path):
            fl = [f for f in r.flags if not f.startswith(("SUPPLIED_AT", "BORROWED_FROM"))]
            lines.append(f"| `{r.path}` | `{r.verdict}` | {', '.join(fl)} |")
        lines.append("")

    leaves = [r for r in matrix if r.verdict != "CONTAINER"]
    by_tier: dict[str, list[MatrixRow]] = defaultdict(list)
    for r in leaves:
        by_tier[r.tier].append(r)

    lines += ["## Matrix by tier (leaf rows)", ""]
    for tier in sorted(by_tier, key=lambda t: (t == "untiered", t)):
        rows = by_tier[tier]
        tally = Counter(r.verdict for r in rows)
        lines += [
            f"### {tier} ({len(rows)} rows)",
            "",
            "  ".join(f"`{v}`×{n}" for v, n in tally.most_common()),
            "",
            "| Path | Verdict | Cost | ARC source | Flags |",
            "|---|---|---|---|---|",
        ]
        for r in sorted(rows, key=lambda r: (VERDICTS.index(r.verdict), r.path)):
            src = ""
            if r.exported:
                src = str(r.exported.get("arc_key", ""))[:60]
            elif r.latent:
                src = str(r.latent.get("arc_location", ""))[:60]
            elif r.borrowed_from:
                src = f"(via `{r.borrowed_from}`)"[:80]
            fl = [f for f in r.flags if not f.startswith("BORROWED_FROM")]
            lines.append(
                f"| `{r.path}` | `{r.verdict}` | {r.cost} | {src} | {', '.join(fl)} |"
            )
        lines.append("")

    conts = sorted((r for r in matrix if r.verdict in {"CONTAINER"}), key=lambda r: r.path)
    lines += [
        f"## Containers ({len(conts)} rows)",
        "",
        "Nested models, roots and union variants. Their verdict is the roll-up of",
        "their descendant leaves (a `BROKEN` container is listed under",
        "Producer-breaking above).",
        "",
        "| Container | Descendant leaf verdicts |",
        "|---|---|",
    ]
    for r in conts:
        roll = ", ".join(f"{v}:{n}" for v, n in sorted((r.rollup or {}).items(), key=lambda kv: -kv[1]))
        lines.append(f"| `{r.path}` | {roll} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--contract-dir", default="docs/contract", type=Path)
    args = ap.parse_args()

    contract_dir = args.contract_dir.resolve()
    if not contract_dir.is_dir():
        sys.exit(f"no such directory: {contract_dir}")

    matrix, stats = build(contract_dir)

    (contract_dir / "GAP_MATRIX.md").write_text(render(matrix, stats))
    (contract_dir / "gap_matrix.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "path": r.path,
                    "verdict": r.verdict,
                    "tier": r.tier,
                    "cost": r.cost,
                    "flags": r.flags,
                    "ruling": r.ruling,
                    "arc_supply": (
                        "unconfirmed" if "ARC_SUPPLY_UNCONFIRMED" in r.flags
                        else "confirmed" if r.verdict in {"ADAPTER_GAP", "WIRED"} else None
                    ),
                    "borrowed_from": r.borrowed_from,
                    "rollup": r.rollup,
                    "checks": r.checks,
                    "demand": r.demand,
                    "exported": r.exported,
                    "latent": r.latent,
                    "adapter": r.adapter,
                    "drift": r.drift,
                }
                for r in matrix
            ],
            sort_keys=False,
            width=100,
        )
    )

    print(f"rows in: {stats['counts']} (A1 raw incl. checks: {stats['raw_counts']['A1']})")
    print(f"matrix rows: {len(matrix)}")
    for v, n in Counter(r.verdict for r in matrix).most_common():
        print(f"  {v:<18} {n}")
    if stats["near_misses"]:
        print(f"\n!! {len(stats['near_misses'])} near-miss path(s) — see GAP_MATRIX.md")
    if stats["missing_inventories"]:
        print(f"\n!! missing: {', '.join(stats['missing_inventories'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
