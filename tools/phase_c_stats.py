#!/usr/bin/env python3
"""Recompute PHASE_C_PLAN.md's §1 figures mechanically from ``gap_matrix.yml``.

Written for the Phase C adversarial-review amendments (see PHASE_C_PLAN.md
history / the review that demoted ARC-0.1, A2 in that review's numbering):
every numerator quoted in §1 turned out to be unreproducible from the matrix.
This script is the reproducible replacement. Re-run it whenever
``gap_matrix.yml`` changes and paste its output back into §1.

Two definitions drive every downstream number, and both are stated here
rather than left implicit:

**Leaf.** A1 (``TCKDB_DEMAND.md`` rule 3) emits one row per nested container
model as well as one per leaf field, so raw row counts overcount. A row is a
*leaf* if no other demand row's path is a strict path-prefix of it (i.e. no
other row's path, taken as a dotted/indexed prefix, extends it). Only rows
that carry a ``demand`` block count as "demand fields" at all -- ``SURPLUS``
and ``ORPHAN_MAPPING`` rows have no demand and are excluded before the
leaf/container split, exactly as PHASE_C_PLAN.md §0.4 does.

**Populated.** A row's verdict is one of eight values. Per PHASE_C_PLAN.md
§0.2, ``SOURCE_UNCONFIRMED`` is 100% real supply once bucketed by the
adapter's own ``maps_from`` (output.yml direct, adapter-asserted constant,
on-disk reparse, sidecar, other-derived) -- none of the 397 rows is a gap.
"Populated" is therefore defined here as ``verdict in {WIRED,
SOURCE_UNCONFIRMED}``. This is the choice that drives the headline
percentage; it is stated so a reader can disagree with it and recompute.
``ARC_ABSENT``, ``ARC_LATENT``, ``ADAPTER_GAP`` and ``BROKEN`` are not
populated -- the field is not present in a deposited record today.

Usage:
    python tools/phase_c_stats.py [--contract-dir docs/contract]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.exit("PyYAML is required: pip install pyyaml")


POPULATED_VERDICTS = {"WIRED", "SOURCE_UNCONFIRMED"}
TIER_ORDER = [
    "tier_1_kinetics_lookup",
    "tier_2_reproducibility",
    "tier_3_atom_mapping",
    "tier_4_completeness",
    "untiered",
]
TIER_LABEL = {
    "tier_1_kinetics_lookup": "tier 1 (kinetics lookup)",
    "tier_2_reproducibility": "tier 2 (reproducibility)",
    "tier_3_atom_mapping": "tier 3 (atom mapping)",
    "tier_4_completeness": "tier 4 (completeness)",
    "untiered": "untiered",
}


def load_rows(contract_dir: Path) -> list[dict]:
    matrix_path = contract_dir / "gap_matrix.yml"
    with matrix_path.open() as fh:
        return yaml.safe_load(fh)


def demand_rows(rows: list[dict]) -> list[dict]:
    """Rows that carry a ``demand`` block -- excludes SURPLUS/ORPHAN_MAPPING."""
    return [r for r in rows if r.get("demand")]


def leaf_rows(rows: list[dict]) -> list[dict]:
    """Demand rows whose path is not a strict path-prefix of another's."""
    d = demand_rows(rows)
    paths = {r["path"] for r in d}

    def is_container(p: str) -> bool:
        for q in paths:
            if q != p and q.startswith(p):
                rest = q[len(p):]
                if rest and rest[0] in ".[":
                    return True
        return False

    return [r for r in d if not is_container(r["path"])]


def su_bucket(row: dict) -> str:
    """Bucket a SOURCE_UNCONFIRMED row by its adapter's ``maps_from``."""
    mf = ((row.get("adapter") or {}).get("maps_from") or "")
    if mf.startswith("output.yml"):
        return "output.yml (direct export contract read)"
    if mf.startswith("on-disk"):
        return "on-disk reparse (artifact bytes/sha256/size)"
    if mf.startswith("tckdb_evidence.json"):
        return "tckdb_evidence.json sidecar"
    if mf.startswith("(none"):
        return "adapter-asserted literal/synthesized"
    return "other derived/computed"


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--contract-dir", default="docs/contract", type=Path)
    args = ap.parse_args()

    rows = load_rows(args.contract_dir)
    d_rows = demand_rows(rows)
    leaves = leaf_rows(rows)

    print("=" * 72)
    print("§0.4 -- container vs. leaf split")
    print("=" * 72)
    print(f"demand rows (rows with a demand block; excludes SURPLUS/ORPHAN_MAPPING): {len(d_rows)}")
    print(f"container rows (strict path-prefix of another demand row):              {len(d_rows) - len(leaves)}")
    print(f"leaf demand fields:                                                     {len(leaves)}")
    print()

    print("=" * 72)
    print("§1.1 -- leaf verdict distribution (the executive-picture table)")
    print("=" * 72)
    vc = Counter(r["verdict"] for r in leaves)
    total = len(leaves)
    populated = sum(vc[v] for v in POPULATED_VERDICTS)
    print(f"{'verdict':<24}{'leaves':>8}{'share':>10}")
    for v in ("WIRED", "SOURCE_UNCONFIRMED", "ARC_ABSENT", "ARC_LATENT", "ADAPTER_GAP", "BROKEN"):
        print(f"{v:<24}{vc.get(v, 0):>8}{pct(vc.get(v, 0), total):>10}")
    print(f"{'-' * 42}")
    print(f"{'POPULATED (WIRED + SOURCE_UNCONFIRMED)':<24}{populated:>8}{pct(populated, total):>10}")
    print(f"{'total leaves':<24}{total:>8}")
    print()

    print("SOURCE_UNCONFIRMED sub-buckets, leaf-restricted (per §0.2's method, informational only):")
    su_leaves = [r for r in leaves if r["verdict"] == "SOURCE_UNCONFIRMED"]
    bc = Counter(su_bucket(r) for r in su_leaves)
    for k, v in bc.most_common():
        print(f"  {v:>4}  {k}")
    print()

    print("=" * 72)
    print("§1.1 -- by tier (leaves; 'populated' = WIRED + SOURCE_UNCONFIRMED)")
    print("=" * 72)
    for t in TIER_ORDER:
        t_leaves = [r for r in leaves if r["tier"] == t]
        t_pop = sum(1 for r in t_leaves if r["verdict"] in POPULATED_VERDICTS)
        print(f"{TIER_LABEL[t]:<28}{t_pop:>5} / {len(t_leaves):<6}{pct(t_pop, len(t_leaves))}")
    print()

    print("=" * 72)
    print("§1.2 -- named populations (leaf path contains the fragment)")
    print("=" * 72)

    def frag_leaves(fragment: str) -> list[dict]:
        return [r for r in leaves if fragment in r["path"]]

    def parameters_leaves() -> list[dict]:
        return [
            r for r in leaves
            if re.search(r"\.parameters\[\]", r["path"]) or r["path"].endswith(".parameters")
        ]

    named = {
        "literature.* (all routes)": frag_leaves(".literature"),
        "applied_energy_corrections[].frequency_scale_factor.*": frag_leaves("frequency_scale_factor"),
        "execution_environment (all routes)": frag_leaves("execution_environment"),
        "calculation.parameters[] (all routes)": parameters_leaves(),
        "reaction_upload.atom_map.* leaves": frag_leaves("atom_map"),
        "geometry.isotopes (all routes)": frag_leaves("isotope"),
    }
    for label, rs in named.items():
        vcount = Counter(r["verdict"] for r in rs)
        detail = ", ".join(f"{v}:{n}" for v, n in vcount.most_common())
        print(f"  {len(rs):>4}  {label:<48} [{detail}]")
    print()

    print("=" * 72)
    print("ARC-7 headline (JobAdapter->record handoff): parameters[] + execution_environment")
    print("=" * 72)
    combined = parameters_leaves() + frag_leaves("execution_environment")
    tc = Counter(r["tier"] for r in combined)
    vcombined = Counter(r["verdict"] for r in combined)
    print(f"total leaves unlocked:        {len(combined)}")
    print(f"  tier 1: {tc.get('tier_1_kinetics_lookup', 0)}   tier 2: {tc.get('tier_2_reproducibility', 0)}")
    print(f"  verdicts: {dict(vcombined)}")
    absent_only = [r for r in combined if r["verdict"] == "ARC_ABSENT"]
    tc_absent = Counter(r["tier"] for r in absent_only)
    print(f"restricted to ARC_ABSENT rows: {len(absent_only)}")
    print(f"  tier 1: {tc_absent.get('tier_1_kinetics_lookup', 0)}   tier 2: {tc_absent.get('tier_2_reproducibility', 0)}")
    print()

    print("=" * 72)
    print("ADAPTER_GAP leaves -- raw verdict count only")
    print("=" * 72)
    gap_leaves = [r for r in leaves if r["verdict"] == "ADAPTER_GAP"]
    print(f"ADAPTER_GAP leaves total: {len(gap_leaves)}")
    print("NOTE: this is the raw matrix verdict, not the semantically-corrected")
    print("'real adapter gap' count. §0.1 resolves each ADAPTER_GAP row (leaf and")
    print("container, 87 total) through its SUPPLIED_AT sibling's exported.arc_key")
    print("to get 12 real gaps / 75 A2-negative rows -- a row-level, not leaf-only,")
    print("population, and a different question than 'what verdict does this leaf")
    print("carry'. That resolution is not a mechanical leaf-only filter (a plain")
    print("exported.arc_key-is-null test on the ADAPTER_GAP rows themselves gives")
    print("5/82, not 12/75, because the real signal lives on the sibling row, not")
    print("on the ADAPTER_GAP row). Left to §0.1's own analysis; not reproduced here.")
    print()

    print("=" * 72)
    print("BROKEN leaves")
    print("=" * 72)
    broken = [r for r in leaves if r["verdict"] == "BROKEN"]
    print(f"BROKEN leaves total: {len(broken)}")
    for r in broken:
        print(f"  {r['path']}")


if __name__ == "__main__":
    main()
