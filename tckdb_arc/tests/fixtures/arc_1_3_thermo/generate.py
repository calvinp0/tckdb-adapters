"""Regenerate output.yml and parser_evidence.json with ARC's real writer (see README.md).

Usage, from an ARC checkout at the PR #1059 head, in an ARC environment:

    PYTHONPATH=$PWD python generate.py /scratch/dir

then copy ``/scratch/dir/output/output.yml`` and ``parser_evidence.json`` here.
"""
import os, sys, shutil, json, copy
from arc.output_schema_test import *
from arc.output_schema_test import make_rich_species, make_transition_state, make_species_corrections
tmp = sys.argv[1]
shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
for lbl in ('sBuOH','nBuOH','NH3'):
    d = os.path.join(tmp,'calcs','Species',lbl,'opt'); os.makedirs(d)
    shutil.copyfile(OPT_LOG, os.path.join(d,'output.out'))
    open(os.path.join(d,'input.gjf'),'w').write(GAUSSIAN_DECK)

s = make_rich_species()
s.t1 = 0.0123
n = make_rich_species()
n.label = 'nBuOH'
n.mol = ARCSpecies(label='nBuOH', smiles='CCCCO').mol
n.rotors_dict = {
    0: {'success': True, 'scan': [1, 2, 3, 4], 'pivots': [2, 3], 'symmetry': 3, 'type': 'HinderedRotor', 'scan_path': '', 'dimensions': 1},
    1: {'success': True, 'scan': [2, 3, 4, 5], 'pivots': [3, 4], 'symmetry': 1, 'type': 'HinderedRotor', 'scan_path': '', 'dimensions': 1},
}
n.arkane_rotor_modes = []      # Arkane dropped every rotor (no force-constant matrix)
n.t1 = None
nh3 = ARCSpecies(label='NH3', smiles='N')
nh3.final_xyz = {'symbols': ('N','H','H','H'), 'isotopes': (14,1,1,1), 'coords': ((0.0,0.0,0.1),(0.9377,0.0,-0.3816),(-0.46885,0.812066,-0.3816),(-0.46885,-0.812066,-0.3816))}
nh3.initial_xyz = nh3.final_xyz
nh3.e_elect = -1.0; nh3.e0 = -0.9; nh3.freqs=[1000.,1600.,1600.,3300.,3400.,3400.]
nh3.optical_isomers=1; nh3.external_symmetry=3; nh3._is_linear=False
nh3.rotors_dict = {}
nh3.arkane_rotor_modes = []
nh3.e0_atom_corrections_applied = True; nh3.e0_bond_corrections_applied = False
nh3.thermo = ThermoData(H298=-45.9, S298=192.8, Tmin=(300.0,'K'), Tmax=(3000.0,'K'),
   thermo_points=[{'temperature_k': 300.0, 'cp_j_mol_k': 35.6, 'h_kj_mol': -45.8, 's_j_mol_k': 193.0, 'g_kj_mol': -103.7}],
   nasa_low={'tmin_k': 300.0, 'tmax_k': 1000.0, 'coeffs': [1.0]*7}, nasa_high={'tmin_k': 1000.0, 'tmax_k': 3000.0, 'coeffs': [2.0]*7})
nh3.thermo.atom_corrections_applied = True; nh3.thermo.bond_corrections_applied = False
nh3.thermo.atom_corrections_level = Level(method='cbs-qb3', software='gaussian')

ts = make_transition_state()
ts.ts_checks = {'E0': True, 'e_elect': True, 'IRC': False, 'freq': True, 'NMD': True, 'warnings': ''}
ts.nmd_record = {'frequency_cm1': -1235.4, 'forced': False}

rxn = ARCReaction(label='sBuOH <=> nBuOH', r_species=[s], p_species=[n], ts_label='TS0')
rxn.multiplicity = 1
rxn.family = 'intra_H_migration'
rxn.long_kinetic_description = 'Fitted by Arkane over 300-3000 K.'
rxn.kinetics = {
    'A': (1.2e5, 's^-1'), 'T0': (1.0, 'K'), 'n': 2.1, 'Ea': (12.3, 'kJ/mol'), 'Tmin': (300.0, 'K'), 'Tmax': (3000.0, 'K'),
    'dA': 1.48, 'dn': 0.05, 'dEa': 0.29, 'dEa_units': 'kJ/mol', 'n_data_points': 50, 'tunneling': 'Eckart',
    'atom_corrections_applied': True,
    'comment': 'Fitted to 50 data points; dA = *|/ 1.48\nTS failed the IRC check: the IRC did not connect the declared wells.',
    'ts_validation': 'TS failed the IRC check: the IRC did not connect the declared wells.',
}
corr = make_species_corrections()
corr['sBuOH']['bac']['value'] = -1.9401   # the applied components sum to the total (output 1.3)
bac = copy.deepcopy(corr['sBuOH']['bac'])
bac['value'] = -1.5615
bac['components'][1].update(parameter_value=None, contribution_value=None)
corr['nBuOH'] = {'aec': copy.deepcopy(corr['sBuOH']['aec']), 'bac': bac}
corr['TS0'] = {'aec': copy.deepcopy(corr['sBuOH']['aec'])}
corr['NH3'] = {'aec': {'value': -0.01, 'value_unit': 'hartree', 'components': [
    {'component_kind':'atom','key':'N','multiplicity':1,'parameter_value':-54.5,'parameter_unit':'hartree','contribution_value':-0.01}]}}
paths = lambda: {'geo': os.path.join(tmp,'calcs','Species','sBuOH','opt','output.out'), 'freq': FREQ_LOG, 'sp': OPT_LOG}

def build(project_directory, species_dict, output_dict, reactions=None, species_corrections=None, **kw):
    with patch('arc.output._compute_point_groups', return_value={l: ('C3v' if l == 'NH3' else 'C1') for l in species_dict}), \
            patch('arc.output._compute_species_corrections', return_value=species_corrections or {}), \
            patch('arc.output._get_arkane_provenance', return_value=ARKANE_PROVENANCE), \
            patch('arc.output._get_rmg_database_identity', return_value=dict(RMG_DATABASE_IDENTITY)), \
            patch('arc.output._get_energy_corrections', return_value=petersson_corrections()):
        write_output_yml(project='schema_test', project_directory=project_directory, species_dict=species_dict,
                         reactions=reactions or [], output_dict=output_dict, **kw)
doc = build(project_directory=tmp,
  species_dict={'sBuOH': s, 'nBuOH': n, 'NH3': nh3, 'TS0': ts},
  output_dict={l: {'convergence': True,'paths': paths(),'job_types': {'opt': True}, 'levels': {'opt': {'method':'wb97xd','basis':'def2tzvp'}, 'freq': {'method':'wb97xd','basis':'def2tzvp'}, 'sp': {'method':'dlpno-ccsd(t)','basis':'cc-pvtz'}}} for l in ('sBuOH','nBuOH','NH3','TS0')},
  reactions=[rxn], species_corrections=corr,
  opt_level=Level(method='wb97xd', basis='def2tzvp', software='gaussian'), freq_level=Level(method='wb97xd', basis='def2tzvp', software='gaussian'),
  sp_level=Level(method='dlpno-ccsd(t)', basis='cc-pvtz', software='orca'),
  arkane_level_of_theory=Level(method='cbs-qb3', software='gaussian'), freq_scale_factor=0.975, bac_type='p')
print(os.listdir(os.path.join(tmp,'output')))
