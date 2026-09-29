#!/usr/bin/env python3
"""Join the Phase A contract inventories into a gap matrix.

Five agents inventory the ARC->TCKDB contract from five angles (see
``docs/contract/FIELD_KEY.md``). This script performs the join between them
mechanically, because the join is bookkeeping over hundreds of rows and a
human or a model doing it by reading will silently drop rows -- and a dropped
row is indistinguishable, downstream, from a real gap. Manufacturing a gap is
the one failure this whole audit exists to avoid.

The most important output is therefore not the matrix but the *near-miss*
report: paths that almost joined. A path spelled ``conformers.geometry`` in
one inventory and ``conformers[].geometry`` in another does not raise -- it
just fails to match, and reports as ARC_ABSENT. Those are flagged loudly for
repair before the matrix is trusted.

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
    "SURPLUS",
    "ORPHAN_MAPPING",
)

# TCKDB reuses sub-models (level_of_theory, software_release, geometry) under
# many parents, and the demand inventory enumerates every route to them. The
# supply inventories key each concept once, at one representative route. A
# demand path with no supply at its own route, but whose sub-model tail is
# supplied elsewhere, is therefore UNDECIDED rather than absent: the supply may
# generalise across routes, or the two instances may be scientifically
# different (a correction's frequency-scale level of theory is not a
# calculation's level of theory). Asserting either way from path shape alone
# would manufacture a verdict, so these are surfaced for adjudication.
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


def tail_index(*indexes: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """Map every sub-model tail of every supplied path back to that path."""
    out: dict[str, list[str]] = defaultdict(list)
    for idx in indexes:
        for path in idx:
            segments = path.split(".")
            for n in range(MIN_TAIL_SEGMENTS, min(MAX_TAIL_SEGMENTS, len(segments)) + 1):
                out[".".join(segments[-n:])].append(path)
    return out


def load_adjudication(contract_dir: Path) -> dict[str, dict[str, Any]]:
    """Load B2's per-concept rulings on the ROUTE_UNRESOLVED population.

    The mechanical join can only observe that a sub-model tail supplied at
    one route is absent at another. Whether that supply *generalises* is a
    scientific judgement -- a correction's frequency-scale level of theory is
    not a calculation's level of theory, even though the path tail is
    identical -- so it is adjudicated separately and applied here.
    """
    path = contract_dir / "ROUTE_ADJUDICATION.yml"
    if not path.exists():
        return {}
    rows = yaml.safe_load(path.read_text()) or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("concept"):
            out[normalise(str(row["concept"]))] = row
    return out


def shared_tail(path: str, sibling: str) -> str:
    """The longest common suffix of two paths -- the sub-model they share."""
    a, b = path.split("."), sibling.split(".")
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return ".".join(a[-n:]) if n else ""


def adjudicate(path: str, sibling: str, rulings: dict[str, dict[str, Any]]) -> str | None:
    """Return GENERALISES / DIFFERENT_INSTANCE for one unresolved row, or None.

    ``PARTIAL`` rulings enumerate the demand paths that do NOT generalise, so
    a path absent from that list is covered by the ruling's general half.
    """
    ruling = rulings.get(normalise(shared_tail(path, sibling)))
    if not ruling:
        return None
    verdict = str(ruling.get("verdict", "")).strip().upper()
    if verdict == "PARTIAL":
        exceptions = ruling.get("exceptions") or []
        if isinstance(exceptions, str):
            exceptions = [exceptions]
        excepted = {normalise(str(e)) for e in exceptions}
        return "DIFFERENT_INSTANCE" if path in excepted else "GENERALISES"
    if verdict in {"GENERALISES", "DIFFERENT_INSTANCE"}:
        return verdict
    return None


def resolve_route(path: str, tails: dict[str, list[str]]) -> str | None:
    """Find the longest sub-model tail of ``path`` supplied at another route.

    Longest-first, so a match on ``level_of_theory.basis`` is preferred over one
    on the barer ``basis`` -- the longer the shared tail, the likelier the two
    routes really do reach the same sub-model.
    """
    segments = path.split(".")
    for n in range(min(MAX_TAIL_SEGMENTS, len(segments)), MIN_TAIL_SEGMENTS - 1, -1):
        candidates = tails.get(".".join(segments[-n:]))
        if candidates:
            return candidates[0]
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
    flags: list[str] = field(default_factory=list)

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
    """Extract (tier_name, glob) pairs from PRIORITY_POLICY.md's YAML block.

    The policy is prose with an embedded fenced block rather than a data file,
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

    List markers are stripped from both sides before matching: the policy is
    written about fields, and whether a field sits under a list is a detail of
    the path, not of its scientific value.
    """
    pattern = re.escape(strip_lists(glob))
    pattern = pattern.replace(r"\*\*\.", r"(?:[^.]+\.)*")  # **. -> any prefix
    if pattern.endswith(r"\.\*"):                          # trailing .* -> subtree
        pattern = pattern[: -len(r"\.\*")] + r"(?:\..+)?"
    pattern = pattern.replace(r"\*", r"[^.]+")             # interior * -> one segment
    return re.compile(pattern)


def strip_lists(path: str) -> str:
    return path.replace("[]", "")


def _specificity(glob: str) -> tuple[int, int]:
    """Rank competing globs so the most specific one claims a path.

    A glob ending in a literal field name beats one ending in a wildcard, which
    is what keeps ``**.note`` (tier 4) from losing every ``note`` in the tree to
    a broad subtree rule like ``reaction_upload.transition_state.*`` (tier 1).
    Ties break on the number of literal segments.
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
    target = strip_lists(path)
    best: tuple[tuple[int, int], str] | None = None
    for tier, glob in tiers:
        if _glob_to_regex(glob).fullmatch(target):
            score = _specificity(glob)
            if best is None or score > best[0]:
                best = (score, tier)
    return best[1] if best else "untiered"


def build(contract_dir: Path) -> tuple[list[MatrixRow], dict[str, Any]]:
    inv = {}
    missing = []
    for agent, filename in INVENTORIES.items():
        rows = load(contract_dir / filename)
        if not rows:
            missing.append(f"{agent} ({filename})")
        inv[agent] = index_by_path(rows)

    tiers = load_tiers(contract_dir)
    rulings = load_adjudication(contract_dir)
    tails = tail_index(inv["A2"], inv["A3"], inv["A4"])
    matrix: list[MatrixRow] = []
    consumed: set[str] = set()

    # One row per TCKDB demand field: the database's wants are the spine of
    # the matrix, since the question being asked is "what can ARC give it".
    for path, demand_rows in sorted(inv["A1"].items()):
        demand = demand_rows[0]
        if len(demand_rows) > 1:
            demand = dict(demand)
            demand["_duplicate_count"] = len(demand_rows)
        exported = inv["A2"].get(path, [None])[0]
        latent = inv["A3"].get(path, [None])[0]
        adapter = inv["A4"].get(path, [None])[0]
        drift = inv["A5"].get(path, [])
        consumed.update({path})

        breaking = [d for d in drift if truthy(d.get("breaking_for_producer"))]

        if adapter and breaking:
            verdict = "BROKEN"
        elif adapter and exported:
            verdict = "WIRED"
        elif exported and not adapter:
            verdict = "ADAPTER_GAP"
        elif latent:
            verdict = "ARC_LATENT"
        elif adapter and not exported:
            # The adapter maps this, but the export-contract inventory has no
            # supply row for it. That is NOT a gap: A2's remit stopped at
            # output.yml and the evidence sidecar, while the adapter also
            # reparses ESS logs through arc.parser. Anything on a reparse path
            # is therefore invisible to A2 by construction. Lumping these in
            # with real gaps overstates the missing surface by an order of
            # magnitude, so they get their own verdict and their own triage:
            # working-but-unattributed, fabricated, or genuinely stale.
            verdict = "SOURCE_UNCONFIRMED"
        else:
            verdict = "ARC_ABSENT"

        sibling = None
        if verdict == "ARC_ABSENT":
            sibling = resolve_route(path, tails)
            if sibling:
                verdict = "ROUTE_UNRESOLVED"
                ruling = adjudicate(path, sibling, rulings)
                if ruling == "GENERALISES":
                    # The sibling's supply covers this route too. Re-verdict
                    # from where that supply actually comes: exported and
                    # mapped is wired; exported but unmapped is an adapter
                    # gap; latent-only means ARC still has to export it.
                    if sibling in inv["A4"] and sibling in inv["A2"]:
                        verdict = "WIRED"
                    elif sibling in inv["A2"]:
                        verdict = "ADAPTER_GAP"
                    elif sibling in inv["A3"]:
                        verdict = "ARC_LATENT"
                    else:
                        verdict = "ARC_ABSENT"
                elif ruling == "DIFFERENT_INSTANCE":
                    # Same sub-model shape, different scientific object: the
                    # sibling says nothing about this route.
                    verdict = "ARC_LATENT" if latent else "ARC_ABSENT"
                row_flags_adjudicated = ruling
            else:
                row_flags_adjudicated = None
        else:
            row_flags_adjudicated = None

        row = MatrixRow(
            path=path,
            verdict=verdict,
            tier=tier_for(path, tiers),
            demand=demand,
            exported=exported,
            latent=latent,
            adapter=adapter,
            drift=drift,
        )

        # Silent-corruption sweep. These outrank every tier (PRIORITY_POLICY),
        # because they pass validation and write wrong science.
        haystack = " ".join(
            str(v)
            for src in (exported, latent, adapter, *drift)
            if src
            for v in src.values()
        ).lower()
        if re.search(r"0-based|1-based|zero-based|one-based|index base|rebase", haystack):
            row.flags.append("INDEX_BASE")
        # Naming a unit is not a risk -- converting between two is. Match
        # conversion language, not the word "unit", or every `*_units` field in
        # the schema flags itself and the signal drowns in its own noise.
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
        if row.verdict == "ARC_ABSENT" and latent is None and exported is None:
            # Distinguish "ARC truly cannot" from "nobody looked".
            row.flags.append("NO_ARC_EVIDENCE")
        if sibling:
            row.flags.append(f"SUPPLIED_AT:{sibling}")
        if row_flags_adjudicated:
            row.flags.append(f"ADJUDICATED:{row_flags_adjudicated}")

        matrix.append(row)

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

    near_misses = find_near_misses(inv)
    stats = {
        "missing_inventories": missing,
        "rulings": len(rulings),
        "counts": {a: sum(len(v) for v in inv[a].values()) for a in INVENTORIES},
        "near_misses": near_misses,
        "tier_patterns": len(tiers),
    }
    return matrix, stats


def find_near_misses(inv: dict[str, dict[str, list]]) -> list[tuple[str, str, str, float]]:
    """Paths that nearly joined against A1 but did not.

    The highest-value output of this script. A near-miss is almost always a
    spelling slip that will otherwise be reported as a real gap.
    """
    demand = list(inv["A1"])
    out = []
    for agent in ("A2", "A3", "A4"):
        for path in inv[agent]:
            if path in inv["A1"] or path.startswith("arc_only."):
                continue
            best, score = None, 0.0
            tail = path.rsplit(".", 1)[-1]
            for cand in demand:
                # Cheap prefilter: sharing a leaf name is a strong signal, and
                # comparing every pair outright is quadratic over ~1e3 paths.
                if cand.rsplit(".", 1)[-1] != tail and not cand.endswith(tail):
                    continue
                s = SequenceMatcher(None, path, cand).ratio()
                if s > score:
                    best, score = cand, s
            if best and score >= NEAR_MISS_RATIO:
                out.append((agent, path, best, round(score, 3)))
    return sorted(out, key=lambda x: -x[3])


def render(matrix: list[MatrixRow], stats: dict[str, Any]) -> str:
    counts = Counter(r.verdict for r in matrix)
    flagged = [r for r in matrix if {"INDEX_BASE", "UNIT_CONVERSION", "SEMANTIC_SHIFT"} & set(r.flags)]

    lines = [
        "# Gap matrix — ARC supply vs TCKDB demand",
        "",
        "Generated by `tools/join_inventories.py`. Do not hand-edit; edit the Phase A",
        "inventories and regenerate. Verdict definitions are in `FIELD_KEY.md`.",
        "",
        "## Verdict summary",
        "",
        "| Verdict | Rows | Meaning |",
        "|---|---:|---|",
    ]
    meaning = {
        "WIRED": "ARC exports it, adapter maps it, survives the 0.22.0 drift",
        "BROKEN": "adapter maps it but the schema moved under it",
        "ADAPTER_GAP": "ARC exports it, TCKDB wants it, adapter drops it",
        "ARC_LATENT": "ARC computes it internally but never exports it",
        "SOURCE_UNCONFIRMED": "adapter maps it, but not from the export contract (reparse? fabricated?)",
        "ARC_ABSENT": "ARC cannot currently produce it",
        "ROUTE_UNRESOLVED": "supplied at another route; does it generalise here?",
        "SURPLUS": "ARC offers it, TCKDB has no home for it",
        "ORPHAN_MAPPING": "adapter targets a field absent from TCKDB 0.22.0",
    }
    for v in VERDICTS:
        if counts.get(v):
            lines.append(f"| `{v}` | {counts[v]} | {meaning.get(v, '')} |")
    lines += ["", f"**Total rows:** {len(matrix)}", ""]

    if stats["missing_inventories"]:
        lines += [
            "> [!WARNING]",
            "> Missing inventories: " + ", ".join(stats["missing_inventories"]) + ".",
            "> The matrix below is INCOMPLETE and its verdicts are not yet trustworthy.",
            "",
        ]

    if stats["near_misses"]:
        lines += [
            "## ⚠ Near-miss paths — repair before trusting the matrix",
            "",
            "These paths nearly matched a TCKDB demand path but did not join. Each is",
            "most likely a spelling slip, and each is currently being reported as a gap",
            "that may not exist.",
            "",
            "| Agent | Path as written | Nearest demand path | Similarity |",
            "|---|---|---|---:|",
        ]
        for agent, path, cand, score in stats["near_misses"]:
            lines.append(f"| {agent} | `{path}` | `{cand}` | {score} |")
        lines.append("")

    if flagged:
        lines += [
            "## Silent-corruption risks",
            "",
            "Ranked above every value tier: these pass validation and write wrong",
            "science. Unit mismatches, atom-index base mismatches, and fields whose",
            "meaning changed under an unchanged name.",
            "",
            "| Path | Verdict | Flags |",
            "|---|---|---|",
        ]
        for r in sorted(flagged, key=lambda r: r.path):
            lines.append(f"| `{r.path}` | `{r.verdict}` | {', '.join(r.flags)} |")
        lines.append("")

    by_tier: dict[str, list[MatrixRow]] = defaultdict(list)
    for r in matrix:
        by_tier[r.tier].append(r)

    lines += ["## Matrix by tier", ""]
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
            lines.append(
                f"| `{r.path}` | `{r.verdict}` | {r.cost} | {src} | {', '.join(r.flags)} |"
            )
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

    print(f"rows in: {stats['counts']}")
    print(f"matrix rows: {len(matrix)}")
    for v, n in Counter(r.verdict for r in matrix).most_common():
        print(f"  {v:<16} {n}")
    if stats["near_misses"]:
        print(f"\n!! {len(stats['near_misses'])} near-miss path(s) — see GAP_MATRIX.md")
    if stats["missing_inventories"]:
        print(f"\n!! missing: {', '.join(stats['missing_inventories'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
