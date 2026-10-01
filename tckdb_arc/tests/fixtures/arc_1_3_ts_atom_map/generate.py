"""Derive ``output.yml`` of this folder from ``../arc_1_3_reactions/output.yml`` (ARC #1059 @ bc731fb4).

ARC's real writer at ebc88ec8 produced the sibling fixture ``../arc_1_3_samples_ebc88ec8`` (an
intramolecular reaction, whose TS atom order follows the reactants). This document covers what that one
cannot, so it is **derived by hand, not written by ARC**: the earlier draft's real document gets the keys ARC
added in ebc88ec8 with values built to satisfy the documented rules, and a second reaction with a repeated
reactant (``CH3 + CH3 <=> C2H6``, its species and TS copied from the real CH3 and TS0 records with the
geometries and identifiers replaced). The result is validated against ARC's
``arc/schemas/output_yml_schema.json`` at ebc88ec8 by ``test_arc_1_3_ts_atom_map.py`` (when that schema is
available), which also checks the semantic invariants the schema cannot express.

Run from anywhere (``python generate.py``); it needs only PyYAML.
"""

import copy
from pathlib import Path

import yaml

HERE = Path(__file__).parent
SOURCE = HERE.parent / "arc_1_3_reactions" / "output.yml"


def xyz(rows):
    return "\n".join(f"{el:<2} {x:14.8f} {y:14.8f} {z:14.8f}" for el, x, y, z in rows)


def main():
    doc = yaml.safe_load(SOURCE.read_text())
    doc["arc_git_commit"] = "ebc88ec8cca0d9c20c8d7e977fed09170088615d"
    species = {s["label"]: s for s in doc["species"]}
    ts0 = doc["transition_states"][0]
    rxn0 = doc["reactions"][0]

    # ---- OH + CH4 <=> H2O + CH3: the TS lists CH4 first, then OH, so the TS order differs from the reactants'.
    # Reactant atoms in ``atom_map_reactant_labels`` order (OH: O H; CH4: C H H H H) are TS atoms
    # [5, 6] + [0, 4, 1, 2, 3]: reactant atom 3 (a CH4 hydrogen) is TS atom 4, the transferring one, which is
    # bonded to O in the product. Products (H2O: O H H; CH3: C H H H) are [5, 6, 4] + [0, 1, 2, 3].
    rxn0["reactant_species_labels"] = ["OH", "CH4"]
    rxn0["product_species_labels"] = ["H2O", "CH3"]
    rxn0["ts_atom_map"] = {
        "ts_label": "TS0",
        "reactants": [5, 6, 0, 4, 1, 2, 3],
        "products": [5, 6, 4, 0, 1, 2, 3],
        "method": "irc_endpoint_cgr_isomorphism",
        "reactant_endpoint": 1,
        "ts_atom_order_follows_reactants": False,
    }
    rxn0["ts_atom_map_unavailable_reason"] = None
    ts0["nmd_forced"] = False
    ts0["reaction_coordinate_mode_index"] = 1       # the -1235.4 cm-1 mode, first in the ESS-order list

    # ---- CH3 + CH3 <=> C2H6: a repeated reactant. ----
    ch3 = species["CH3"]
    c2h6 = copy.deepcopy(ch3)
    c2h6_rows = [("C", 0.0, 0.0, 0.7620), ("C", 0.0, 0.0, -0.7620),
                 ("H", 1.0192, 0.0, 1.1551), ("H", -0.5096, 0.8827, 1.1551), ("H", -0.5096, -0.8827, 1.1551),
                 ("H", -1.0192, 0.0, -1.1551), ("H", 0.5096, 0.8827, -1.1551), ("H", 0.5096, -0.8827, -1.1551)]
    c2h6.update(
        label="C2H6", smiles="CC", formula="C2H6", multiplicity=1,
        inchi="InChI=1S/C2H6/c1-2/h1-2H3", inchi_key="OTMSDBZUPAUEDD-UHFFFAOYSA-N",
        xyz=xyz(c2h6_rows), xyz_isotopes=[12, 12, 1, 1, 1, 1, 1, 1],
        opt_input_xyz=xyz(c2h6_rows), opt_input_xyz_isotopes=[12, 12, 1, 1, 1, 1, 1, 1],
        opt_log="calcs/C2H6/opt.out", freq_log="calcs/C2H6/freq.out", sp_log="calcs/C2H6/sp.out",
        sp_energy_hartree=-79.6, opt_final_energy_hartree=-79.3)
    c2h6["sp_spin_diagnostic"] = None
    c2h6["scf_reference"] = copy.deepcopy(ch3["scf_reference"])
    doc["species"].append(c2h6)

    # The TS lists the second CH3's carbon first: reactants (CH3 occ 1: C H H H; CH3 occ 2: C H H H) are TS
    # atoms [1, 2, 3, 4] + [0, 5, 6, 7]. C2H6's atoms are C C H H H H H H, so ``atom_map`` sends the second
    # carbon to product atom 1: products are TS atoms [1, 0, 2, 3, 4, 5, 6, 7].
    ts1 = copy.deepcopy(ts0)
    ts1_rows = [("C", 0.0, 0.0, -0.9), ("C", 0.0, 0.0, 0.9),
                ("H", 1.0192, 0.0, 1.4), ("H", -0.5096, 0.8827, 1.4), ("H", -0.5096, -0.8827, 1.4),
                ("H", -1.0192, 0.0, -1.4), ("H", 0.5096, 0.8827, -1.4), ("H", 0.5096, -0.8827, -1.4)]
    ts1.update(
        label="TS1", formula="C2H6", multiplicity=1, rxn_label="CH3 + CH3 <=> C2H6",
        xyz=xyz(ts1_rows), xyz_isotopes=[12, 12, 1, 1, 1, 1, 1, 1],
        opt_log="calcs/TS1/opt.out", freq_log="calcs/TS1/freq.out", sp_log="calcs/TS1/sp.out",
        irc_logs=["calcs/TS1/irc_1.out", "calcs/TS1/irc_2.out"],
        freq_frequencies_cm1_ess_order=[-480.0, 90.0, 300.0, 500.0, 900.0, 1300.0, 1500.0, 3000.0, 3100.0, 3200.0],
        imag_freq_cm1=-480.0, imaginary_frequencies_cm1=[-480.0],
        # A forced pass: the normal mode displacement check failed and ``skip_nmd`` made it pass.
        nmd_forced=True, reaction_coordinate_mode_index=None, sp_energy_hartree=-79.5, opt_final_energy_hartree=-79.2)
    ts1["ts_checks"] = {"E0": True, "IRC": True, "NMD": True, "e_elect": True, "freq": True, "warnings": ""}
    ts1["irc_participant_mapping"] = {
        "atom_order_matches_ts": True, "sides_distinguishable": True,
        "reactants": {"endpoint": 1, "endpoint_label": "IRC_TS1_1", "participants": [
            {"label": "CH3", "occurrence": 1, "position": 1, "atom_indices": [1, 2, 3, 4]},
            {"label": "CH3", "occurrence": 2, "position": 2, "atom_indices": [0, 5, 6, 7]}]},
        "products": {"endpoint": 2, "endpoint_label": "IRC_TS1_2", "participants": [
            {"label": "C2H6", "occurrence": 1, "position": 1, "atom_indices": [0, 1, 2, 3, 4, 5, 6, 7]}]},
    }
    doc["transition_states"].append(ts1)
    rxn1 = {
        "label": "CH3 + CH3 <=> C2H6", "reactant_labels": ["CH3"], "product_labels": ["C2H6"],
        "reactant_species_labels": ["CH3", "CH3"], "product_species_labels": ["C2H6"],
        "family": "R_Recombination", "multiplicity": 1, "ts_label": "TS1", "kinetics": None, "reversible": True,
        "atom_map": [0, 2, 3, 4, 1, 5, 6, 7],
        "atom_map_reactant_labels": ["CH3", "CH3"], "atom_map_product_labels": ["C2H6"],
        "atom_map_source": "inferred",
        "atom_map_method": "arc.mapping.driver.map_reaction (family: R_Recombination)",
        "ts_atom_map": {
            "ts_label": "TS1", "reactants": [1, 2, 3, 4, 0, 5, 6, 7], "products": [1, 0, 2, 3, 4, 5, 6, 7],
            "method": "irc_endpoint_cgr_isomorphism", "reactant_endpoint": 1,
            "ts_atom_order_follows_reactants": False},
        "ts_atom_map_unavailable_reason": None,
    }
    doc["reactions"].append(rxn1)

    # ---- Screened conformers of CH4 with mixed provenance (the new conformer_ess_* lists). ----
    ch4 = species["CH4"]
    base = [line.split() for line in ch4["xyz"].splitlines()]

    def shifted(dx):
        return "\n".join(f"{r[0]:<2} {float(r[1]) + dx:14.8f} {float(r[2]):14.8f} {float(r[3]):14.8f}" for r in base)

    level = {"method": "wb97xd", "basis": "def2svp", "method_type": "dft"}
    ch4.update(
        conformers=[shifted(0.005), shifted(0.01), shifted(0.02), shifted(0.03)],
        conformers_isotopes=[[12, 1, 1, 1, 1]] * 4,
        # 0: Gaussian optimization with an energy; 1: ORCA optimization, no energy; 2: a level was recorded but
        # its log was lost (no program); 3: a force-field geometry.
        conformer_energies=[-306000.0, None, -305990.0, None],
        conformer_levels=[level, level, level, None],
        conformer_ess_software=["gaussian", "orca", None, None],
        conformer_ess_version=["Gaussian 09, Revision D.01", "ORCA 6.0.1", None, None],
        conformer_energy_kind="electronic_kj_mol", conformer_energy_level=level, conformer_force_field=None)

    (HERE / "output.yml").write_text(yaml.safe_dump(doc, sort_keys=True, default_flow_style=False))


if __name__ == "__main__":
    main()
