"""Shared plumbing for the opt-in live TCKDB integration gate.

Everything here is offline: the corpus builders and the adapter factory (the
loopback guard and the commit probe live in ``tckdb_core.testing.live``). Network
access happens only through the ``live_tckdb`` fixture in ``conftest.py``, which calls
``assert_loopback_url`` first.

The corpora are the offline fixtures the unit suite already uses, laid out as
ARC project directories (``output/output.yml`` plus any evidence sidecar), so
the adapter reads them exactly as it reads a finished ARC run. The variants
below add only what no shipped fixture carries: kinetics with a non-unit
reference temperature, Arkane provenance, per-species energy corrections,
an uploadable current-ARC (``parser_evidence``) species, and composition-
consistent geometries for the synthetic reaction.
"""

import copy
import json
import shutil
from pathlib import Path

import yaml

from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBArtifactConfig, TCKDBConfig

# The guards and the commit probe are producer-agnostic and live in the core test kit.
from tckdb_core.testing.live import (  # noqa: F401  (re-exported: the tests import them from here)
    API_KEY_ENV,
    INTEGRATION_BUCKET_PREFIX,
    LOOPBACK_HOSTS,
    REFUSED_PORTS,
    assert_integration_backend,
    assert_loopback_url,
    commit_probe,
    verify_isolated_backend,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
GOLDEN = FIXTURES / "golden"
CURRENT_ARC = FIXTURES / "current_arc"
ARC_1_2 = FIXTURES / "arc_1_2" / "output.yml"
ARC_1_3_LEVELS = FIXTURES / "arc_1_3_levels"
TS_ATOM_MAP_DERIVED = FIXTURES / "arc_1_3_ts_atom_map" / "output.yml"
TS_ATOM_MAP_SAMPLE = FIXTURES / "arc_1_3_samples_ebc88ec8" / "reaction_kinetics.output.yml"
# A real Gaussian log shipped with the package, used only as artifact bytes.
SAMPLE_LOG = Path(__file__).resolve().parents[2] / "testing" / "irc" / "rxn_1_irc_1.out"

ARKANE_GIT_COMMIT = "7f4e9c2a1b3d5e6f708192a3b4c5d6e7f8091a2b"

# ARC kinetics with T0 = 300 K: the prefactor is stored as sent (A), with ``t0_k`` = 300 (tckdb-schemas 0.63).
KINETICS_T0 = {
    "A": 2.5e13, "A_units": "cm^3/(mol*s)", "n": 1.5, "T0_k": 300.0,
    "Ea": 40.0, "Ea_units": "kJ/mol", "Tmin_k": 300.0, "Tmax_k": 2000.0,
    "dA": 1.3, "dn": 0.05, "dEa": 0.4, "dEa_units": "kJ/mol",
    "n_data_points": 30, "tunneling": "Eckart",
}








def _load_yaml(path):
    return yaml.safe_load(Path(path).read_text())


def golden():
    return _load_yaml(GOLDEN / "phase3_output.yml"), [GOLDEN / "tckdb_evidence.json"]


def golden_kinetics():
    doc, sidecars = golden()
    doc["reactions"][0]["kinetics"] = dict(KINETICS_T0)
    doc["arkane_git_commit"] = ARKANE_GIT_COMMIT
    return doc, sidecars


def level_args():
    """golden + kinetics with ARC-shaped ``args`` on every level (see ``tests/_level_args.py``).

    TCKDB 0.158 refuses ``level_of_theory.keywords``; the adapter used to flatten ``args`` into it. The opt and
    freq levels are Gaussian (``int=dkh2``, ``scf=(maxcycle=200)``), the sp level and a correction scheme's are
    ORCA (``tightpno rijcosx``). The adapter sends no typed qualifier from them yet, so the stored levels state none.
    """
    from _level_args import level_args_corpus

    return level_args_corpus()


def _golden_with_irc_verdict(verdict):
    """golden + kinetics with ARC's ``ts_checks`` recording an IRC verdict.

    ``irc_converged`` only says the IRC jobs finished (ARC ``arc/output.py``
    ``_ts_checks_to_dict``); ``ts_checks['IRC']`` is ARC's verdict on whether
    the IRC connects the declared reactants and products. ``freq`` is left unassessed: a stated verdict
    would also deposit an ``imaginary_mode`` record (0.64), and these corpora read back the IRC evidence alone.
    """
    doc, sidecars = golden_kinetics()
    doc["transition_states"][0]["ts_checks"] = {
        "E0": None, "e_elect": None, "IRC": verdict, "freq": None, "NMD": None, "warnings": "",
    }
    return doc, sidecars


def golden_ts_evidence():
    """golden + kinetics with every TS verdict ARC states, so each evidence kind is sent (tckdb-schemas 0.64).

    ``freq`` and ``e_elect`` are True: the TS carries one imaginary mode (-900 cm^-1) and its sp energy
    (-1.6 Eh) is above both sides' summed participant sp energies (-1.67 Eh), at the one shared sp level.
    """
    doc, sidecars = golden_kinetics()
    doc["transition_states"][0]["ts_checks"] = {
        "E0": None, "e_elect": True, "IRC": True, "freq": True, "NMD": None, "warnings": "",
    }
    return doc, sidecars


def golden_irc_passed():
    return _golden_with_irc_verdict(True)


def golden_irc_failed():
    return _golden_with_irc_verdict(False)


def _without_staged_routes(doc):
    """Drop the route lines of a fixture whose logs are other levels' logs (not part of this gate's question).

    The ARC 1.3 fixtures name calculation logs that do not exist, and their recorded routes are the
    placeholders ARC's sample writer chose, so the adapter would report ``level_contradicted_by_route``.
    The atom-map corpora are about the reaction mapping, not routes (as in ``test_arc_1_3_ts_atom_map``).
    """
    for record in (*doc["species"], *doc["transition_states"]):
        for key in ("opt_route", "freq_route", "sp_route"):
            if key in record:
                record[key] = None
        if record.get("irc_log_routes"):
            record["irc_log_routes"] = [None] * len(record["irc_log_routes"])
    return doc


def ts_atom_map_sample():
    """ARC's own writer at ebc88ec8: ``nC3H7 <=> iC3H7`` with a real ``ts_atom_map`` (TS order follows the reactants)."""
    return _without_staged_routes(_load_yaml(TS_ATOM_MAP_SAMPLE)), []


def ts_atom_map_derived():
    """The derived fixture: ``OH + CH4 <=> H2O + CH3`` (TS order unlike the reactants') and ``CH3 + CH3 <=> C2H6``
    (a repeated reactant), both with a ``ts_atom_map`` and ``reactant_species_labels``."""
    return _without_staged_routes(_load_yaml(TS_ATOM_MAP_DERIVED)), []


def arc_1_2():
    return _load_yaml(ARC_1_2), []


def arc_1_2_corrections():
    """arc_1_2 CH4 with an sp result and ARC's tool-neutral correction records."""
    doc = _load_yaml(ARC_1_2)
    doc["arkane_git_commit"] = ARKANE_GIT_COMMIT
    lot = {"method": "wb97x-d", "basis": "def2-tzvp", "software": "gaussian"}
    ch4 = next(s for s in doc["species"] if s["label"] == "CH4")
    ch4["sp_energy_hartree"] = -40.51
    ch4["ess_versions"]["sp"] = "Gaussian 16"
    # Before output 1.3 the thermo block says which correction record its run applied, and the adapter
    # deposits no record the run did not apply; the shipped CH4 states bond corrections off.
    ch4["thermo"]["bond_corrections_applied"] = True
    ch4["energy_corrections"] = [
        {"correction_type": "atom_energy", "model": "arkane_atom_energy",
         "level_of_theory": lot, "total": {"value": -0.0234, "unit": "hartree"},
         "components": [
             {"component_kind": "atom", "key": "C", "multiplicity": 1,
              "parameter_value": -37.847, "parameter_unit": "hartree",
              "contribution_value": -0.0153},
             {"component_kind": "atom", "key": "H", "multiplicity": 4,
              "parameter_value": -0.4998, "parameter_unit": "hartree",
              "contribution_value": -0.0081}]},
        {"correction_type": "bond_additivity", "model": "petersson",
         "level_of_theory": lot, "total": {"value": -0.694, "unit": "kcal_mol"},
         "components": [
             {"component_kind": "bond", "key": "C-H", "multiplicity": 4,
              "parameter_value": -0.1735, "parameter_unit": "kcal_mol",
              "contribution_value": -0.694}],
         "parameter_table": {"unit": "kcal_mol", "values": {"C-H": -0.1735}}},
    ]
    doc["species"] = [ch4]
    return doc, []


def arc_1_3_isotopes():
    """ARC 1.3's deuterated iC3H7 beside its ordinary isotopologue, with their parsed Hessians (parser evidence) and a rotor scan each.

    Since tckdb-schemas 0.94 a Hessian's geometry, and every scan point's, is held to the isotopes of
    the species its calculation is filed under, so the substituted species' Hessian and scan points
    must carry ``isotopes`` (``{"4": 2}``). The scans are Gaussian scans (an order-preserving program)
    whose samples state no isotopes (``geometry_isotopes`` null, as a Gaussian opt-type scan log
    does): the adapter labels each point of the deuterated species from the record's own isotope
    list, and drops the geometry of one frame (the third) whose element order is not the
    record's. The shipped fixture stages other species' logs, so route lines are dropped as for the
    atom-map corpora.
    """
    doc = _load_yaml(ARC_1_3_LEVELS / "output.yml")
    doc["species"] = [s for s in doc["species"] if s["label"] in ("iC3H7", "iC3H7_d")]
    doc["transition_states"], doc["reactions"] = [], []
    for record in doc["species"]:
        for key in ("opt_route", "freq_route", "sp_route"):
            record[key] = None
        frame = record["xyz"].strip().splitlines()
        swapped = [frame[3], *frame[1:3], frame[0], *frame[4:]]          # C and an H exchanged: another element order
        samples = [
            {"source_index": i, "angle_degrees": 60.0 * i, "relative_energy_kj_mol": float(i),
             "electronic_energy_hartree": -118.0 - 0.001 * i,
             "geometry_xyz": "\n".join(lines), "geometry_isotopes": None}
            for i, lines in enumerate([frame, frame, swapped])]
        record["rotor_scans"] = [{
            "key": "scan_rotor_0", "ess_software": "gaussian", "ess_version": "Gaussian 09, Revision D.01",
            "source_log": None,
            "result": {"dimension": 1, "relaxed": True,
                       "coordinate": {"coordinate_type": "dihedral", "atom_indices": [1, 2, 3, 4],
                                      "index_base": 1, "unit": "degree"},
                       "samples": samples}}]
    return doc, [ARC_1_3_LEVELS / "parser_evidence.json"]


def current_arc_h2o():
    """The current-ARC fixture's H2O made uploadable, geometry in the Hessian frame.

    The shipped fixture is a parser-evidence corpus with no levels, energies or
    convergence flags, so it builds no payload on its own; its TS0 IRC and GSM
    geometries are not the same molecule, so only the species is usable.
    """
    doc = _load_yaml(CURRENT_ARC / "output.yml")
    evidence = json.loads((CURRENT_ARC / "parser_evidence.json").read_text())
    doc["parser_evidence"] = {k: evidence[k] for k in ("schema_name", "schema_version", "document_id")}
    doc["parser_evidence"]["path"] = "parser_evidence.json"
    hessian = next(r for r in evidence["records"] if r["label"] == "H2O")["freq_hessian"]["value"]
    doc.update(
        project="current_arc_h2o",
        opt_level={"method": "wb97xd", "basis": "def2-svp", "software": "orca"},
        freq_level={"method": "wb97xd", "basis": "def2-svp", "software": "orca"},
        sp_level={"method": "wb97xd", "basis": "def2-tzvp", "software": "orca"},
        transition_states=[],
    )
    doc["species"][0].update(
        smiles="O", charge=0, multiplicity=1, is_ts=False, converged=True,
        xyz="\n".join(hessian["geometry_xyz_text"].splitlines()[2:]),
        opt_n_steps=6, opt_final_energy_hartree=-76.3, opt_converged=True,
        freq_n_imag=0, zpe_hartree=0.021, sp_energy_hartree=-76.4,
        ess_versions={"opt": "ORCA 5.0.4", "freq": "ORCA 5.0.4", "sp": "ORCA 5.0.4"},
    )
    return doc, [CURRENT_ARC / "parser_evidence.json"]


_SYNTHETIC_XYZ = {
    "CHO": "C 0.0 0.0 0.0\nO 1.18 0.0 0.0\nH -0.55 0.95 0.0",
    "CH4": ("C 0.0 0.0 0.0\nH 0.629 0.629 0.629\nH -0.629 -0.629 0.629\n"
            "H -0.629 0.629 -0.629\nH 0.629 -0.629 -0.629"),
    "CH2O": "C 0.0 0.0 0.0\nO 1.21 0.0 0.0\nH -0.55 0.94 0.0\nH -0.55 -0.94 0.0",
    "CH3": "C 0.0 0.0 0.0\nH 1.08 0.0 0.0\nH -0.54 0.935 0.0\nH -0.54 -0.935 0.0",
    "TS0": ("C 0.0 0.0 0.0\nO -1.18 0.0 0.0\nH 0.55 0.95 0.0\nH 1.40 -0.40 0.0\n"
            "C 2.70 -0.80 0.0\nH 3.10 -0.30 0.89\nH 3.10 -0.30 -0.89\nH 2.90 -1.87 0.0"),
}


def synthetic_reaction():
    """The unit suite's CHO + CH4 reaction doc, with real compositions.

    Its shipped geometries are all two atoms ("C" + "H"), which the server
    refuses as a composition mismatch; see :func:`synthetic_species_as_shipped`.
    """
    from test_thermo_enthalpy_declaration import _reaction_doc_with_thermo

    doc = _reaction_doc_with_thermo()
    for record in [*doc["species"], *doc["transition_states"]]:
        record["converged"] = True
        record["xyz"] = _SYNTHETIC_XYZ[record["label"]]
    doc["reactions"][0]["kinetics"]["T0_k"] = 300.0
    return doc, []


def mixed_spin_reaction():
    """The r3_04 shape: radicals computed unrestricted, closed-shell species computed restricted (tckdb-schemas 0.161).

    ``CHO + CH4 <=> CH2O + CH3`` over ``synthetic_reaction``'s compositions, at B3LYP/def2-TZVP: CHO, CH3 and the
    doublet transition state ran unrestricted (their stated ``<S**2>`` 0.76), CH4 and CH2O restricted. The linked
    energies are therefore at two resolved levels, which TCKDB 0.158 refused any single declaration for; from 0.161
    the open-shell reference (``unrestricted``) is the declaration and the restricted singlets do not constrain it.
    """
    doc, sidecars = synthetic_reaction()
    level = {"method": "b3lyp", "basis": "def2-tzvp", "software": "gaussian"}
    for name in ("opt_level", "freq_level", "sp_level", "scan_level"):
        doc[name] = dict(level)
    for record in [*doc["species"], *doc["transition_states"]]:
        open_shell = record["multiplicity"] > 1
        record["scf_reference"] = {
            "freq_reference": "unrestricted" if open_shell else "restricted",
            "sp_reference": "unrestricted" if open_shell else "restricted",
        }
        record["sp_spin_diagnostic"] = {"s_squared": 0.76, "s_squared_expected": 0.75} if open_shell else None
    return doc, sidecars


_ONE_ELECTRON_XYZ = {
    "H": "H 0.0 0.0 0.0",
    "CH3NH2": ("C 0.000 0.000 0.000\nN 1.465 0.000 0.000\nH -0.370 1.020 0.000\nH -0.370 -0.510 0.885\n"
               "H -0.370 -0.510 -0.885\nH 1.830 0.470 0.810\nH 1.830 0.470 -0.810"),
    "H2": "H 0.0 0.0 0.37\nH 0.0 0.0 -0.37",
    "CH3NH": ("C 0.000 0.000 0.000\nN 1.400 0.000 0.000\nH -0.370 1.020 0.000\nH -0.370 -0.510 0.885\n"
              "H -0.370 -0.510 -0.885\nH 1.800 0.900 0.000"),
    "TS0": ("C 0.000 0.000 0.000\nN 1.465 0.000 0.000\nH -0.370 1.020 0.000\nH -0.370 -0.510 0.885\n"
            "H -0.370 -0.510 -0.885\nH 1.830 0.470 0.810\nH 1.830 0.470 -0.810\nH 2.350 -0.900 0.000"),
}


def one_electron_hf_reaction(hydride=False):
    """The rxn_210 shape: a free H atom run at HF under a DLPNO-CCSD(T)-F12 reaction (tckdb-schemas 0.173).

    ``[H] + CN <=> [H][H] + C[NH]``: every participant and the transition state at ORCA
    DLPNO-CCSD(T)-F12/cc-pVTZ-F12 (aux ``aug-cc-pvtz/c``, CABS ``cc-pvtz-f12-cabs``), unrestricted for the radicals and
    the doublet TS, restricted for the singlets; ORCA cannot run a correlated method on one electron, so the H atom
    (a one-atom species, no opt or freq) is ``method: hf`` with the same basis, aux and CABS and ``sp_route:
    uhf hf cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightpno``. With ``hydride`` the atom is H- (charge -1, a
    singlet, two electrons), so the HF energy is not the correlated one: ``[H-] + CN <=> [H][H] + C[NH-]``.
    """
    from test_orca_route_aux_basis import ORCA_SP, _orca_doc  # noqa: PLC0415

    doc = _orca_doc()
    radical, closed = copy.deepcopy(doc["species"][3]), copy.deepcopy(doc["species"][1])
    ts = doc["transition_states"][0]
    charge = -1 if hydride else 0
    spec = [
        ("H", "[H-]" if hydride else "[H]", charge, 1 if hydride else 2, closed if hydride else radical),
        ("CH3NH2", "CN", 0, 1, closed),
        ("H2", "[H][H]", 0, 1, closed),
        ("CH3NH", "C[NH-]" if hydride else "C[NH]", charge, 1 if hydride else 2, closed if hydride else radical),
    ]
    energies = {"H": -0.5, "CH3NH2": -95.8, "H2": -1.17, "CH3NH": -95.1}
    species = []
    for label, smiles, chg, mult, template in spec:
        record = copy.deepcopy(template)
        record.update(label=label, smiles=smiles, charge=chg, multiplicity=mult, xyz=_ONE_ELECTRON_XYZ[label],
                      sp_energy_hartree=energies[label], opt_log=f"calcs/{label}/opt.log",
                      freq_log=f"calcs/{label}/freq.log", sp_log=f"calcs/{label}/sp.log")
        species.append(record)
    atom = species[0]
    atom["levels"].update(opt=None, freq=None)
    atom.update(opt_log=atom["sp_log"], freq_log=None, opt_route=None, freq_route=None, xyz_isotopes=[1],
                opt_final_energy_hartree=-0.5, zpe_hartree=None)
    atom["ess_software"].update(opt=None, freq=None)
    atom["ess_versions"] = {"sp": "ORCA 6.0.0"}
    atom["levels"]["sp"] = {**ORCA_SP, "method": "hf"}
    atom["sp_route"] = "uhf hf cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightpno"
    ts.update(label="TS0", charge=charge, multiplicity=1 if hydride else 2, xyz=_ONE_ELECTRON_XYZ["TS0"],
              sp_energy_hartree=-96.3, opt_final_energy_hartree=-96.3)
    doc["species"] = species
    for record in (*doc["species"], *doc["transition_states"]):
        record["converged"] = True
        open_shell = record["multiplicity"] > 1
        record["scf_reference"] = {"freq_reference": "unrestricted" if open_shell else "restricted",
                                   "sp_reference": "unrestricted" if open_shell else "restricted"}
        record["sp_spin_diagnostic"] = ({"s_squared": 0.76, "s_squared_expected": 0.75} if open_shell else None)
    atom["sp_spin_diagnostic"] = {"s_squared": 0.75, "s_squared_expected": 0.75} if not hydride else None
    reaction = doc["reactions"][0]
    reaction.update(label=f"{species[0]['label']} + CH3NH2 <=> H2 + CH3NH", multiplicity=1 if hydride else 2,
                    reactant_labels=["H", "CH3NH2"], product_labels=["H2", "CH3NH"],
                    atom_map_reactant_labels=["H", "CH3NH2"], atom_map_product_labels=["H2", "CH3NH"])
    reaction["kinetics"]["T0_k"] = 300.0
    return doc, []


def hydride_one_electron_reaction():
    return one_electron_hf_reaction(hydride=True)


def synthetic_species_as_shipped():
    from test_adapter import _fake_output_doc, _full_record

    doc = _fake_output_doc()
    doc["species"] = [_full_record()]
    return doc, []


# trans-2-butene (dihedral C-C=C-C = 180.0) and its cis counterpart (0.0), RDKit ETKDG + MMFF geometries.
_BUTENE_TRANS_XYZ = """C 1.94530151 0.07760137 -0.11139993
C 0.49146346 0.04375807 -0.45446639
C -0.49146344 -0.04375657 0.45446602
C -1.94530157 -0.07760140 0.11139995
H 2.45878937 -0.76612915 -0.58239661
H 2.11760294 0.02276106 0.96812703
H 2.39391265 1.00426955 -0.48210723
H 0.24679252 0.09477651 -1.51329148
H -0.24679251 -0.09477411 1.51329132
H -2.45879166 0.76612631 0.58239965
H -2.39390959 -1.00427154 0.48210456
H -2.11760368 -0.02276008 -0.96812688"""
_BUTENE_CIS_XYZ = """C 1.56435786 -0.47685207 0.12094652
C 0.71257244 0.71442196 -0.17616989
C -0.62772423 0.78533326 -0.19597906
C -1.60881536 -0.30896831 0.07404659
H 2.20553193 -0.26509107 0.98237247
H 2.20836398 -0.69202499 -0.73751579
H 0.99100414 -1.37871662 0.34387544
H 1.26763788 1.62477154 -0.40123437
H -1.07677897 1.74880803 -0.43588381
H -1.14062468 -1.26593803 0.31236910
H -2.24917984 -0.02940503 0.91653054
H -2.24634515 -0.45633867 -0.80335773"""
BUTENE_SMILES = "C/C=C/C"  # trans (E)


def _butene(label, xyz):
    """arc_1_2's opt-only CH4 record turned into 2-butene with the given geometry (no thermo: CH4's is not its)."""
    doc = _load_yaml(ARC_1_2)
    record = next(s for s in doc["species"] if s["label"] == "CH4")
    record.update(label=label, smiles=BUTENE_SMILES, xyz=xyz, thermo=None)
    doc["species"] = [record]
    return doc, []


def stereo_consistent():
    """SMILES ``C/C=C/C`` (E) with a geometry that is trans: TCKDB 0.138 accepts it with no ``species_stereo_contradiction``."""
    return _butene("trans-2-butene", _BUTENE_TRANS_XYZ)


def stereo_contradicting():
    """The same SMILES with the cis geometry: TCKDB 0.138 refuses it, 422 ``species_stereo_contradiction``."""
    return _butene("trans-2-butene-cis-geometry", _BUTENE_CIS_XYZ)


# Gaussian freq routes: a bare ``freq`` (no directive) and ``freq=numer`` (finite difference of gradients), each paired with the method ARC states.
HESSIAN_DECKS = {
    "sBuOH": ("freq", "analytic"),
    "nBuOH": ("freq=numer", "finite_difference_gradient"),
}
HESSIAN_DECK_PATH = "calcs/Species/{label}/freq/input.gjf"


def hessian_deck():
    """arc_1_3_thermo's two butanols, each shipping a REAL Gaussian freq input whose route agrees with the stated method.

    TCKDB 0.144 reads the deck itself and refuses a stated ``freq.hessian_method`` that the deck contradicts. The
    adapter forwards ARC's value and the corpus keeps it equal to the deck's reading (a bare ``freq`` states none,
    ``freq=numer`` states ``finite_difference_gradient``). The staged logs of the fixture are other levels' logs, so
    their routes and files are dropped as for the atom-map corpora; only the freq deck is real.
    """
    doc = _load_yaml(FIXTURES / "arc_1_3_thermo" / "output.yml")
    doc["species"] = [s for s in doc["species"] if s["label"] in HESSIAN_DECKS]
    doc["transition_states"], doc["reactions"] = [], []
    for record in doc["species"]:
        keyword, method = HESSIAN_DECKS[record["label"]]
        for key in ("opt_route", "freq_route", "sp_route"):
            record[key] = None
        for key in ("opt_log", "opt_input", "freq_log", "sp_log", "sp_input"):
            record[key] = None
        record["freq_input"] = HESSIAN_DECK_PATH.format(label=record["label"])
        record["freq_hessian_method"] = method
    return doc, []


def hessian_deck_files(doc):
    """The freq decks of :func:`hessian_deck` as ``{relative path: text}`` (a Gaussian input for each species)."""
    files = {}
    for record in doc["species"]:
        keyword, _ = HESSIAN_DECKS[record["label"]]
        body = "\n".join(line for line in record["xyz"].strip().splitlines())
        files[record["freq_input"]] = (
            f"%mem=2GB\n%nprocshared=4\n#P wb97xd/def2tzvp {keyword}\n\n{record['label']} freq\n\n0 1\n{body}\n\n")
    return files


# Corpora whose projects need files on disk beside output.yml: ``{corpus: builder(doc) -> {relative path: text}}``.
PROJECT_FILES = {"hessian_deck": hessian_deck_files}


CORPORA = {
    "golden": golden,
    "golden_kinetics": golden_kinetics,
    "level_args": level_args,
    "golden_ts_evidence": golden_ts_evidence,
    "golden_irc_passed": golden_irc_passed,
    "golden_irc_failed": golden_irc_failed,
    "ts_atom_map_sample": ts_atom_map_sample,
    "ts_atom_map_derived": ts_atom_map_derived,
    "arc_1_2": arc_1_2,
    "arc_1_2_corrections": arc_1_2_corrections,
    "arc_1_3_isotopes": arc_1_3_isotopes,
    "current_arc_h2o": current_arc_h2o,
    "synthetic_reaction": synthetic_reaction,
    "mixed_spin_reaction": mixed_spin_reaction,
    "one_electron_hf_reaction": one_electron_hf_reaction,
    "hydride_one_electron_reaction": hydride_one_electron_reaction,
    "stereo_consistent": stereo_consistent,
    "stereo_contradicting": stereo_contradicting,
    "hessian_deck": hessian_deck,
}


def materialize(root, name):
    """Write corpus ``name`` as an ARC project under ``root``; return (dir, doc)."""
    builder = CORPORA.get(name) or {"synthetic_species_as_shipped": synthetic_species_as_shipped}[name]
    doc, sidecars = builder()
    project = Path(root) / name
    (project / "output").mkdir(parents=True, exist_ok=True)
    (project / "output" / "output.yml").write_text(yaml.safe_dump(doc, sort_keys=False))
    for sidecar in sidecars:
        shutil.copyfile(sidecar, project / "output" / Path(sidecar).name)
    for relative, text in PROJECT_FILES.get(name, lambda doc: {})(doc).items():
        (project / relative).parent.mkdir(parents=True, exist_ok=True)
        (project / relative).write_text(text)
    return project, copy.deepcopy(doc)


def attach_log(project, doc, label):
    """Give species ``label`` an opt log and input deck on disk (artifact bytes)."""
    calc_dir = Path(project) / "calcs" / "Species" / label / "opt"
    calc_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE_LOG, calc_dir / "output.log")
    (calc_dir / "input.gjf").write_text("#P opt wb97xd/def2svp\n\nH2\n\n0 1\nH 0 0 0\nH 0 0 0.74\n\n")
    record = next(s for s in doc["species"] if s["label"] == label)
    record["opt_log"] = str((calc_dir / "output.log").relative_to(project))
    record["opt_input"] = str((calc_dir / "input.gjf").relative_to(project))
    (Path(project) / "output" / "output.yml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return doc


def make_config(live_url, name, mode, *, artifacts=False):
    return TCKDBConfig(
        enabled=True,
        base_url=assert_loopback_url(live_url),
        api_key_env=API_KEY_ENV,
        payload_dir="tckdb_payloads",
        project_label=f"integ-{name}",
        upload_mode=mode,
        upload=True,
        artifacts=TCKDBArtifactConfig(upload=artifacts, kinds=("output_log", "input")),
    )


def make_adapter(live_url, project, name, mode, **kwargs):
    return TCKDBAdapter(make_config(live_url, name, mode, **kwargs), project_directory=project)




def read_sidecars(project):
    """Current (non-archived) upload sidecars under a project's payload dir."""
    return {
        path.name: json.loads(path.read_text())
        for path in sorted((Path(project) / "tckdb_payloads").rglob("*.meta.json"))
        if "archive" not in path.parts
    }
