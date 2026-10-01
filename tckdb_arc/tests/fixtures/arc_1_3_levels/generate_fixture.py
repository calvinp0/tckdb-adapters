"""Regenerate ``output.yml`` and ``parser_evidence.json`` of this folder with ARC's real writer.

Run from the root of an ARC worktree at PR #1059 (schema 1.3), with ARC importable::

    conda run -n arc_env python <this folder>/generate_fixture.py

Everything the writer reads is a real log from ``arc/testing`` staged into a scratch
project (so the exported paths are run-relative) or an ARC object built the way
``arc/output_schema_test.py`` builds them; the four shell-outs into the RMG environment
are replaced exactly as ``arc.output_schema_test.build_output_document`` replaces them,
so point groups, correction tables and Arkane provenance are fixtures, not measurements.
The document covers batch E of the 1.3 adapter work: per-species ``levels`` under
``adaptive_levels``, the header job levels, a composite run, screened conformers with
electronic energies and a force-field geometry, IRC endpoint species, a TS with IRC
jobs and an xtb GSM path search, an isotopically substituted species, rotor scans with
their own program, and the route lines.

The result is validated against ``arc/schemas/output_yml_schema.json``.
"""

import json
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ARC_ROOT = os.getcwd()
if not os.path.isfile(os.path.join(ARC_ROOT, 'arc', 'output.py')):
    sys.exit('Run this script from the root of the ARC worktree.')
sys.path.insert(0, ARC_ROOT)

from jsonschema import Draft202012Validator

from arc.common import ARC_TESTING_PATH as T, read_yaml_file
from arc.level import Level
from arc.output import EnergyCorrections, write_output_yml
from arc.output_schema_test import (ARKANE_PROVENANCE, OUTPUT_SCHEMA_PATH, RMG_DATABASE_IDENTITY,
                                    make_rich_species)
from arc.parser import parser
from arc.reaction.reaction import ARCReaction
from arc.species.species import ARCSpecies, TSGuess

SMALL = {'method': 'b3lyp', 'basis': '6-31g'}
BIG = {'method': 'wb97xd', 'basis': 'def2tzvp'}
SINGLE_POINT = {'method': 'ccsd(t)-f12', 'basis': 'cc-pvtz-f12'}
CONFORMER = {'method': 'wb97xd', 'basis': 'def2svp'}
COMPOSITE = {'method': 'cbs-qb3'}


def stage(project_dir, label, job, source, kind='Species', name='output.out'):
    directory = os.path.join(project_dir, 'calcs', kind, label, job)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    shutil.copyfile(source, path)
    return path


def recorded_levels(**kwargs):
    levels = {key: None for key in ('opt', 'freq', 'sp', 'composite', 'irc')}
    levels.update(kwargs)
    return levels


def species_from_logs(label, smiles, project_dir, *, isotopes=None):
    opt_log = stage(project_dir, label, 'opt', os.path.join(T, 'opt', 'iC3H7.out'))
    freq_log = stage(project_dir, label, 'freq', os.path.join(T, 'freq', 'iC3H7.out'))
    xyz = parser.parse_geometry(opt_log)
    if isotopes is not None:
        xyz = dict(xyz, isotopes=tuple(isotopes))
    # An isotopically labelled species is built from its SMILES alone (ARC re-perceives the molecule
    # from a given xyz and drops the isotope labels from it).
    spc = ARCSpecies(label=label, smiles=smiles) if isotopes is not None \
        else ARCSpecies(label=label, smiles=smiles, xyz=xyz)
    spc.initial_xyz = xyz if isotopes is not None else spc.get_xyz()
    spc.final_xyz = xyz
    spc.e_elect = parser.parse_e_elect(opt_log)
    spc.freqs = list(parser.parse_frequencies(freq_log))
    spc.optical_isomers = 1
    spc.external_symmetry = 1
    spc._is_linear = False
    spc.arkane_rotor_modes = []
    return spc, opt_log, freq_log


def perturbed(xyz, scale):
    """A different conformer geometry of the same species (coordinates scaled about the origin)."""
    return dict(xyz, coords=tuple(tuple(c * scale for c in row) for row in xyz['coords']))


def endpoints_from_ts(ts_xyz):
    import numpy as np
    symbols, coords = ts_xyz['symbols'], np.array(ts_xyz['coords'], dtype=float)
    carbons = [i for i, s in enumerate(symbols) if s == 'C']
    hydrogens = [i for i, s in enumerate(symbols) if s == 'H']
    migrating = min(hydrogens, key=lambda h: sum(sorted(np.linalg.norm(coords[h] - coords[c])
                                                        for c in carbons)[:2]))
    near = sorted(carbons, key=lambda c: np.linalg.norm(coords[migrating] - coords[c]))[:2]
    out = []
    for carbon in near:
        moved = coords.copy()
        direction = coords[migrating] - coords[carbon]
        moved[migrating] = coords[carbon] + 1.09 * direction / np.linalg.norm(direction)
        out.append({'symbols': tuple(symbols), 'coords': tuple(tuple(r) for r in moved)})
    return out[1], out[0]


def build(project_dir):
    species, output = dict(), dict()

    # Adaptive levels: two species of one run at different levels (the header levels are run defaults).
    small, opt_log, freq_log = species_from_logs('iC3H7', 'C[CH]C', project_dir)
    species['iC3H7'] = small
    output['iC3H7'] = {'convergence': True, 'job_types': {'opt': True},
                       'paths': {'geo': opt_log, 'freq': freq_log, 'sp': opt_log},
                       'levels': recorded_levels(opt=SMALL, freq=SMALL, sp=SMALL)}

    big, opt_log, freq_log = species_from_logs('nC3H7', '[CH2]CC', project_dir)
    xyz = big.final_xyz
    big.conformers = [xyz, perturbed(xyz, 1.01), perturbed(xyz, 1.02)]
    big.conformer_energies = [-307146.96, -307143.0, None]
    big.conformer_levels = [dict(CONFORMER, software='gaussian'), dict(CONFORMER, software='gaussian'), None]
    big.conformer_energy_sources = [
        {'kind': 'electronic_kj_mol', 'level': dict(CONFORMER, software='gaussian')},
        {'kind': 'electronic_kj_mol', 'level': dict(CONFORMER, software='gaussian')},
        None]
    sp_log = stage(project_dir, 'nC3H7', 'sp', os.path.join(T, 'opt', 'iC3H7.out'))
    species['nC3H7'] = big
    output['nC3H7'] = {'convergence': True, 'job_types': {'opt': True, 'sp': True},
                       'paths': {'geo': opt_log, 'freq': freq_log, 'sp': sp_log},
                       'levels': recorded_levels(opt=BIG, freq=BIG, sp=SINGLE_POINT)}

    # A force-field-only conformer set (no conformer was ESS-optimized).
    ff, opt_log, freq_log = species_from_logs('nC3H7_ff', '[CH2]CC', project_dir)
    ff.conformers = [perturbed(ff.final_xyz, 1.01), perturbed(ff.final_xyz, 1.02)]
    ff.conformer_energies = [-2.5, -2.4]
    ff.conformer_energy_sources = [{'kind': 'force_field_kcal_mol', 'force_field': 'MMFF94s (rdkit)'}] * 2
    species['nC3H7_ff'] = ff
    output['nC3H7_ff'] = {'convergence': True, 'job_types': {'opt': True},
                          'paths': {'geo': opt_log, 'freq': freq_log, 'sp': opt_log},
                          'levels': recorded_levels(opt=BIG, freq=BIG, sp=BIG)}

    # A composite run: only the composite log is exported.
    comp_log = stage(project_dir, 'SO2OO', 'composite', os.path.join(T, 'composite', 'SO2OO_CBS-QB3.log'))
    comp_xyz = parser.parse_geometry(comp_log)
    comp = ARCSpecies(label='SO2OO', smiles='O=S1(=O)OO1', xyz=comp_xyz)
    comp.final_xyz = comp_xyz
    comp.e_elect = parser.parse_e_elect(comp_log)
    comp.freqs = list(parser.parse_frequencies(comp_log))
    comp.optical_isomers = 1
    comp.external_symmetry = 1
    comp._is_linear = False
    comp.arkane_rotor_modes = []
    species['SO2OO'] = comp
    output['SO2OO'] = {'convergence': True, 'job_types': {'composite': True, 'opt': True, 'sp': True},
                       'paths': {'composite': comp_log}, 'levels': recorded_levels(composite=COMPOSITE)}

    # An isotopically substituted species: one methyl hydrogen is deuterium.
    deut, opt_log, freq_log = species_from_logs(
        'iC3H7_d', '[2H]C[CH]C', project_dir, isotopes=[12, 12, 12, 2, 1, 1, 1, 1, 1, 1])
    species['iC3H7_d'] = deut
    output['iC3H7_d'] = {'convergence': True, 'job_types': {'opt': True},
                         'paths': {'geo': opt_log, 'freq': freq_log, 'sp': opt_log},
                         'levels': recorded_levels(opt=SMALL, freq=SMALL, sp=SMALL)}

    # Rotor scans with their own program (ARC's rich species).
    rich = make_rich_species()
    rich_opt = stage(project_dir, 'sBuOH', 'opt', os.path.join(T, 'opt', 'iC3H7.out'))
    scan_log = stage(project_dir, 'sBuOH', 'scan', os.path.join(T, 'rotor_scans', 'sBuOH.out'))
    for rotor in rich.rotors_dict.values():
        if rotor.get('scan_path'):
            rotor['scan_path'] = scan_log
    species['sBuOH'] = rich
    output['sBuOH'] = {'convergence': True, 'job_types': {'opt': True},
                       'paths': {'geo': rich_opt, 'freq': os.path.join(T, 'freq', 'iC3H7.out'), 'sp': rich_opt},
                       'levels': recorded_levels(opt=BIG, freq=BIG, sp=BIG)}

    # A reaction: wells, TS with IRC jobs and an xtb GSM path search, and the two IRC endpoint species.
    ts_opt = stage(project_dir, 'TS0', 'opt', os.path.join(T, 'opt', 'TS_nC3H7-iC3H7.out'), kind='TSs')
    ts_freq = stage(project_dir, 'TS0', 'freq', os.path.join(T, 'freq', 'TS_nC3H7-iC3H7.out'), kind='TSs')
    irc_1 = stage(project_dir, 'TS0', 'irc_1', os.path.join(T, 'irc', 'rxn_1_irc_1.out'), kind='TSs')
    irc_2 = stage(project_dir, 'TS0', 'irc_2', os.path.join(T, 'irc', 'rxn_1_irc_2.out'), kind='TSs')
    gsm_source = os.path.join(T, 'parser_evidence', 'gsm', 'stringfile.xyz0000')
    gsm = stage(project_dir, 'TS0', 'gsm', gsm_source, kind='TSs', name='stringfile.xyz0000')
    node_dir = os.path.join(os.path.dirname(gsm), 'gsm_node_outputs')
    shutil.copytree(os.path.join(os.path.dirname(gsm_source), 'gsm_node_outputs'), node_dir, dirs_exist_ok=True)
    for node in ('0000.01', '0000.02'):
        with open(os.path.join(node_dir, f'{node}.xtbout'), 'w') as handle:
            handle.write(" |                           x T B                           |\n"
                         "      * xtb version 6.7.1 (edcfbbe) compiled by 'conda@fixture' on 2024-01-01\n"
                         f"      program call               : xtb {node}.xyz --chrg 0 --uhf 1 --gfn 2\n"
                         "          Hamiltonian                  GFN2-xTB\n")
    ts_xyz = parser.parse_geometry(ts_opt)
    ts = ARCSpecies(label='TS0', is_ts=True, xyz=ts_xyz, rxn_label='nC3H7 <=> iC3H7', multiplicity=2)
    ts.initial_xyz = ts_xyz
    ts.final_xyz = ts_xyz
    ts.e_elect = parser.parse_e_elect(ts_opt)
    ts.freqs = list(parser.parse_frequencies(ts_freq))
    ts.optical_isomers = 1
    ts.external_symmetry = 1
    ts._is_linear = False
    ts.arkane_rotor_modes = []
    ts.chosen_ts = 0
    ts.chosen_ts_method = 'xtb_gsm'
    ts.successful_methods = ['xtb_gsm']
    guess = TSGuess(method='xtb_gsm', xyz=ts_xyz)
    guess.index = 0
    guess.method_sources = ['xtb_gsm']
    ts.ts_guesses = [guess]
    ts.irc_label = 'IRC_TS0_1 IRC_TS0_2'
    species['TS0'] = ts
    irc_level = {'method': 'wb97xd', 'basis': 'def2tzvp'}
    output['TS0'] = {'convergence': True, 'job_types': {'opt': True, 'irc': True},
                     'paths': {'geo': ts_opt, 'freq': ts_freq, 'sp': ts_opt, 'irc': [irc_1, irc_2], 'gsm': gsm,
                               'irc_directions': ['forward', 'reverse'], 'irc_levels': [irc_level, irc_level]},
                     'levels': recorded_levels(opt=BIG, freq=BIG, sp=BIG, irc=irc_level)}
    endpoint_1, endpoint_2 = endpoints_from_ts(ts_xyz)
    for label, xyz, direction in (('IRC_TS0_1', endpoint_1, 'forward'), ('IRC_TS0_2', endpoint_2, 'reverse')):
        endpoint = ARCSpecies(label=label, xyz=xyz, multiplicity=2)
        endpoint.final_xyz = xyz
        endpoint.irc_label = 'TS0'
        species[label] = endpoint
        output[label] = {'convergence': True, 'job_types': {'opt': True}, 'paths': {},
                         'levels': recorded_levels(), 'irc_direction': direction}

    rxn = ARCReaction(label='nC3H7 <=> iC3H7', r_species=[big], p_species=[small], ts_label='TS0')
    rxn.ts_species = ts
    rxn.multiplicity = 2
    rxn.family = 'intra_H_migration'
    rxn.kinetics = {'A': (3.1e9, 's^-1'), 'n': 1.2, 'Ea': (142.0, 'kJ/mol'), 'T0': (1.0, 'K'),
                    'Tmin': (300.0, 'K'), 'Tmax': (2000.0, 'K'), 'dA': 1.48, 'dn': 0.05, 'dEa': 0.29,
                    'dEa_units': 'kJ/mol', 'n_data_points': 50, 'tunneling': 'Eckart',
                    'atom_corrections_applied': True, 'comment': None, 'ts_validation': None}
    return species, [rxn], output


def main():
    scratch = tempfile.mkdtemp()
    try:
        project_dir = os.path.join(scratch, 'levels13')
        species, reactions, output = build(project_dir)
        key = "LevelOfTheory(method='bmk',basis='cbsb7',software='gaussian')"
        corrections = EnergyCorrections(aec={'C': -37.8, 'H': -0.5}, bac={'C-H': -0.17, 'C-C': -0.36},
                                        aec_key=key, bac_key=key)
        levels = {'opt_level': Level(method='uhf', basis='3-21g', software='gaussian'),
                  'freq_level': Level(method='uhf', basis='3-21g', software='gaussian'),
                  'sp_level': Level(method='uhf', basis='3-21g', software='gaussian')}
        adaptive = {(1, 2): {('opt', 'freq', 'sp'): Level(method='b3lyp', basis='6-31g', software='gaussian')},
                    (3, 'inf'): {('opt', 'freq'): Level(method='wb97xd', basis='def2tzvp', software='gaussian'),
                                 ('sp',): Level(method='ccsd(t)-f12', basis='cc-pvtz-f12', software='molpro')}}
        with patch('arc.output._compute_point_groups', return_value={label: 'C1' for label in species}), \
                patch('arc.output._compute_species_corrections', return_value=dict()), \
                patch('arc.output._get_arkane_provenance', return_value=ARKANE_PROVENANCE), \
                patch('arc.output._get_rmg_database_identity', return_value=dict(RMG_DATABASE_IDENTITY)), \
                patch('arc.output._get_energy_corrections', return_value=corrections):
            write_output_yml(
                project='levels13', project_directory=project_dir, species_dict=species, reactions=reactions,
                output_dict=output, compute_thermo=False, t0=1700000000.0,
                scan_level=Level(method='b3lyp', basis='6-31g', software='gaussian'),
                irc_level=Level(method='wb97xd', basis='def2tzvp', software='gaussian'),
                conformer_opt_level=Level(method='wb97xd', basis='def2svp', software='gaussian'),
                conformer_sp_level=Level(method='ccsd(t)-f12', basis='cc-pvtz-f12', software='molpro'),
                ts_guess_level=Level(method='gfn2', software='xtb'),
                composite_method=Level(method='cbs-qb3', software='gaussian'),
                arkane_level_of_theory=Level(method='bmk', basis='cbsb7', software='gaussian'),
                adaptive_levels=adaptive, freq_scale_factor=1.0, freq_scale_factor_user_provided=True,
                **levels)
        out_dir = os.path.join(project_dir, 'output')
        for name in ('output.yml', 'parser_evidence.json'):
            source = os.path.join(out_dir, name)
            if os.path.isfile(source):
                shutil.copyfile(source, os.path.join(HERE, name))
        # The GSM stringfile and its archived xtb outputs, the only logs this fixture ships.
        shutil.copytree(os.path.join(project_dir, 'calcs', 'TSs', 'TS0', 'gsm'),
                        os.path.join(HERE, 'calcs', 'TSs', 'TS0', 'gsm'), dirs_exist_ok=True)
        with open(OUTPUT_SCHEMA_PATH) as handle:
            schema = json.load(handle)
        document = read_yaml_file(os.path.join(HERE, 'output.yml'))
        errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda e: list(e.path))
        print('VALID' if not errors else f'INVALID ({len(errors)} errors)', document['schema_version'])
        for error in errors[:10]:
            print('   ', '/'.join(str(step) for step in error.path), '->', error.message[:200])
        sys.exit(1 if errors else 0)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == '__main__':
    main()
