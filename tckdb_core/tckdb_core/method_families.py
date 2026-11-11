"""Classification of a method name into a family, replicated from TCKDB (stop-gap).

Used for one rule: TCKDB's one-electron Hartree-Fock exemption in the kinetics energy-level
check applies only when the declared level's method is a ``wavefunction`` or ``multireference``
method (so DFT, named composites and unclassified names get none). The server decides that with
``app.chemistry.method_families.classify_method``, which is not part of ``tckdb_schemas``.

STOP-GAP: this module is a faithful copy of that classifier (family only, no DFT rung, no
version registry), checked against the backend by
``tests/test_method_families.py`` when ``TCKDB_BACKEND_PATH`` points at its ``backend/``.
TODO: once tckdb_schemas ships the classifier (requested), drop this module and import it
instead (expected: ``from tckdb_schemas.chemistry.method_families import classify_method``,
exact path to be confirmed against that release).

Replicated from TCKDB ``app/chemistry/method_families.py`` (classification version 1): the name
goes through :func:`tckdb_core.level_rules.method_identity_key`; the exact key is tried first,
then the key without a trailing dispersion suffix (with the ``b97`` stem override), then without a
leading ``ro``/``r``/``u`` spin prefix. A name the table does not hold is ``unclassified``; a
method is never guessed into a family.
"""

from __future__ import annotations

import re
from enum import Enum

from tckdb_core.level_rules import method_identity_key


class Family(str, Enum):
    dft = "dft"
    wavefunction = "wavefunction"
    composite = "composite"
    multireference = "multireference"
    semiempirical = "semiempirical"
    force_field = "force_field"
    unclassified = "unclassified"


#: Density functionals by rung. Keys are method identity keys (lower case, after the curated aliases), with no trailing
#: dispersion suffix.
DFT_BY_RUNG: dict[int, tuple[str, ...]] = {
    # 1: local density approximation
    1: ("lda", "lsda", "svwn", "svwn5", "svwn3", "vwn", "slater", "hfs", "xalpha"),
    # 2: generalised gradient approximation, no exact exchange
    2: (
        "pbe",
        "pbepbe",
        "blyp",
        "bp86",
        "bpbe",
        "bpw91",
        "pw91",
        "pwpw91",
        "pw91pw91",
        "revpbe",
        "rpbe",
        "pbesol",
        "olyp",
        "xlyp",
        "b97-d",
        "b97-d2",
        "b97-d3",
        "b97-d3bj",
        "b97-d4",
        "b97d",
        "b97d3",
        "b97-3c",
        "hcth",
        "hcth407",
        "hcth120",
        "hcth147",
        "n12",
        "sogga11",
        "mpwpw91",
    ),
    # 3: meta-GGA, no exact exchange
    3: (
        "tpss",
        "tpsstpss",
        "revtpss",
        "scan",
        "rscan",
        "r2scan",
        "r2scan-3c",
        "m06-l",
        "m06l",
        "m11-l",
        "mn12-l",
        "mn15-l",
        "vsxc",
        "b97m-v",
        "b97m",
        "m11l",
        "mn12l",
        "mn15l",
        "tau-hcth",
        "tauhcth",
    ),
    # 4: hybrid: global, range-separated and meta-hybrid
    4: (
        "b3lyp",
        "b3lyp5",
        "ub3lyp",
        "rb3lyp",
        "rob3lyp",
        "b3pw91",
        "b3p86",
        "b1lyp",
        "b1b95",
        "pbe0",
        "pbe1pbe",
        "pbeh1pbe",
        "pbeh-3c",
        "revpbe0",
        "b97",
        "b97-1",
        "b97-2",
        "b971",
        "b972",
        "b98",
        "b3lyp-d",
        "m08hx",
        "mn12sx",
        "n12sx",
        "sogga11x",
        "lc-whpbe",
        "lrc-ωpbeh",
        "ωb97xd",
        "wb97x-3c",
        "bhandh",
        "bhandhlyp",
        "bhlyp",
        "mpw1pw91",
        "mpw1k",
        "mpw1lyp",
        "mpw1pbe",
        "mpw1b95",
        "x3lyp",
        "o3lyp",
        "apfd",
        "tpssh",
        "tpss0",
        "tpss1kcis",
        "revtpssh",
        "scan0",
        "r2scan0",
        "r2scanh",
        "r2scan50",
        "pw6b95",
        "pwb6k",
        "bmk",
        "m05",
        "m05-2x",
        "m052x",
        "m06",
        "m06-hf",
        "m06hf",
        "m062x",
        "m08-hx",
        "m08-so",
        "m11",
        "mn12-sx",
        "mn15",
        "sogga11-x",
        "cam-b3lyp",
        "camb3lyp",
        "lc-wpbe",
        "lcwpbe",
        "lc-ωpbe",
        "lc-blyp",
        "lrc-wpbe",
        "lrc-wpbeh",
        "hse03",
        "hse06",
        "hse",
        "hseh1pbe",
        "wb97",
        "wb97x",
        "wb97xd",
        "wb97x-v",
        "wb97m",
        "wb97m-v",
        "ωb97",
        "ωb97x",
        "ωb97x-d",
        "ωb97x-v",
        "ωb97m-v",
    ),
    # 5: double hybrid
    5: (
        "b2plyp",
        "b2plypd",
        "b2plypd3",
        "b2plyp-d",
        "b2gpplyp",
        "b2gp-plyp",
        "b2k-plyp",
        "b2t-plyp",
        "mpw2plyp",
        "dsd-blyp",
        "dsd-pbep86",
        "dsd-pbeb95",
        "revdsd-pbep86",
        "revdsd-pbepbe",
        "pwpb95",
        "pbe0-dh",
        "pbe0-2",
        "pbe-qidh",
    ),
}

#: Every other family. Same key rules as :data:`DFT_BY_RUNG`.
OTHER_BY_FAMILY: dict[Family, tuple[str, ...]] = {
    Family.wavefunction: (
        "hf",
        "rhf",
        "uhf",
        "rohf",
        "mp2",
        "ump2",
        "rmp2",
        "romp2",
        "mp2.5",
        "mp3",
        "mp4",
        "mp4(sdq)",
        "mp4(sdtq)",
        "scs-mp2",
        "sos-mp2",
        "lmp2",
        "ri-mp2",
        "df-mp2",
        "dlpno-mp2",
        "mp2-f12",
        "df-mp2-f12",
        "ccd",
        "cc2",
        "cc3",
        "ccsd",
        "ccsd(t)",
        "ccsdt",
        "ccsdt(q)",
        "ccsdtq",
        "rccsd(t)",
        "uccsd(t)",
        "lccsd(t)",
        "ccsd-f12",
        "ccsd-f12a",
        "ccsd-f12b",
        "ccsd(t)-f12",
        "ccsd(t)-f12a",
        "ccsd(t)-f12b",
        "ccsd(t)-f12c",
        "ccsd(t*)-f12",
        "ccsd(t*)-f12a",
        "ccsd(t*)-f12b",
        "rccsd(t)-f12",
        "uccsd(t)-f12",
        "pno-lccsd(t)-f12",
        "dlpno-ccsd",
        "dlpno-ccsd(t)",
        "dlpno-ccsd(t1)",
        "dlpno-ccsd(t)-f12",
        "cisd",
        "qcisd",
        "qcisd(t)",
        "bd",
        "bd(t)",
        "bccd",
        "qci",
        "fci",
    ),
    Family.multireference: (
        "mcscf",
        "casscf",
        "rasscf",
        "caspt2",
        "ms-caspt2",
        "xms-caspt2",
        "rs2",
        "rs2c",
        "nevpt2",
        "sc-nevpt2",
        "pc-nevpt2",
        "mrpt2",
        "rs3",
        "acpf",
        "aqcc",
        "cic",
        "mrcic",
        "mrci",
        "mrci+q",
        "mrci(q)",
        "mrcisd",
        "mrcisd+q",
        "mrci-f12",
        "mrci-f12+q",
        "icmrci",
        "icmrci+q",
    ),
    Family.composite: (
        "g1",
        "g2",
        "g3",
        "g4",
        "g2mp2",
        "g3mp2",
        "g3b3",
        "g3mp2b3",
        "g4mp2",
        "g3x",
        "cbs-qb3",
        "rocbs-qb3",
        "cbs-4m",
        "cbs-apno",
        "w1",
        "w1u",
        "w1bd",
        "w1ro",
        "w2",
        "w2u",
        "w3",
        "w4",
        "heat",
        "heat-345q",
        "heat-345(q)",
        "heat-345qp",
    ),
    Family.semiempirical: (
        "am1",
        "pm3",
        "pm6",
        "pm6-dh2",
        "pm7",
        "rm1",
        "mndo",
        "indo",
        "cndo",
        "xtb",
        "gfn0-xtb",
        "gfn1-xtb",
        "gfn2-xtb",
        "dftb",
        "dftb2",
        "dftb3",
        "scc-dftb",
    ),
    Family.force_field: (
        "mm2",
        "mm3",
        "mm4",
        "mm+",
        "mmff",
        "mmff94",
        "mmff94s",
        "uff",
        "gaff",
        "gaff2",
        "amber",
        "opls",
        "opls-aa",
        "charmm",
        "dreiding",
        "reaxff",
        "gfn-ff",
        "gfnff",
    ),
}

#: Trailing dispersion corrections that never change a class. Longest alternatives first is not needed: the pattern is
#: anchored at the end and the alternatives are whole tokens.
DISPERSION_SUFFIXES: tuple[str, ...] = (
    "d2",
    "d3",
    "d3bj",
    "d3zero",
    "d3(bj)",
    "d3(0)",
    "d3m",
    "d3mbj",
    "d4",
    "gd2",
    "gd3",
    "gd3bj",
)
#: ``(stem)-(suffix)`` over a method key; the replacement keeps the stem. A pattern needs a non-empty stem.
DISPERSION_STRIP_PATTERN: str = "^(.+)-(?:" + "|".join(re.escape(s) for s in DISPERSION_SUFFIXES) + ")$"
_DISPERSION_STRIP = re.compile(DISPERSION_STRIP_PATTERN, re.DOTALL)


def _build_table() -> dict[str, Family]:
    table: dict[str, Family] = {}

    def add(key: str, family: Family) -> None:
        if key in table:
            raise ValueError(f"method family table: {key!r} is listed twice")
        table[key] = family

    for keys in DFT_BY_RUNG.values():
        for key in keys:
            add(key, Family.dft)
    for family, keys in OTHER_BY_FAMILY.items():
        for key in keys:
            add(key, family)
    return table


#: The table, one row per method key.
CLASSIFICATION: dict[str, Family] = _build_table()

#: A stem that, once a dispersion suffix has been stripped from it, names a different method than the bare stem does.
SUFFIX_STEM_OVERRIDES: dict[str, str] = {"b97": "b97-d"}

#: Spin prefixes read through, longest first.
SPIN_PREFIXES: tuple[str, ...] = ("ro", "r", "u")

#: Families for which TCKDB exempts a one-electron Hartree-Fock link under the declared level.
ONE_ELECTRON_EXEMPT_FAMILIES = frozenset({Family.wavefunction, Family.multireference})


def method_stem_key(name: str) -> str:
    """The key a method is classified under (identity key, else dispersion-stripped, else spin-prefix-stripped)."""
    key = method_identity_key(name)
    if key in CLASSIFICATION:
        return key
    stem = _DISPERSION_STRIP.sub(r"\1", key)
    stem = method_identity_key(stem) if stem != key else key
    if stem != key:
        stem = SUFFIX_STEM_OVERRIDES.get(stem, stem)
    if stem in CLASSIFICATION:
        return stem
    for prefix in SPIN_PREFIXES:
        if stem.startswith(prefix) and len(stem) > len(prefix):
            rest = method_identity_key(stem[len(prefix):])
            if rest in CLASSIFICATION:
                return rest
    return stem


def method_family(name: str | None) -> Family:
    """The family of a method name; ``unclassified`` for ``None``, blank, or a name the table does not hold."""
    if not isinstance(name, str) or not name.strip():
        return Family.unclassified
    return CLASSIFICATION.get(method_stem_key(name), Family.unclassified)
