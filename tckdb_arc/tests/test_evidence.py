import json
from pathlib import Path

import pytest

from tckdb_arc.evidence import EvidenceStore, validate_output_schema


DOC_ID = "0123456789abcdef0123456789abcdef"
XYZ = "2\npoint\nH 0.0 0.0 0.0\nH 0.0 0.0 0.7\n"


def output_doc():
    return {
        "schema_version": "1.1",
        "species": [{"label": "H2"}],
        "transition_states": [{"label": "TS0"}],
        "tckdb_evidence": {
            "path": "tckdb_evidence.json",
            "schema_name": "arc-tckdb-evidence",
            "schema_version": "1.0",
            "document_id": DOC_ID,
        },
    }


def hessian_value():
    return {
        "source_log": "calcs/H2/freq/output.log",
        "geometry_xyz_text": XYZ,
        "atom_count": 2,
        "matrix_dimension": 6,
        "packing": "lower_triangle_row_major_including_diagonal",
        "units": "hartree_per_bohr_squared",
        "source": "parsed_log",
        "parser_version": "arc-hessian-1",
        "lower_triangle": [float(i) for i in range(21)],
    }


def irc_value():
    return {
        "parser_version": "arc-irc-path-1",
        "trajectories": [{
            "source_log": "calcs/TS0/irc/output.log",
            "declared_direction": "forward",
            "points": [{
                "source_point_index": 1,
                "direction": "forward",
                "geometry_xyz_text": XYZ,
                "electronic_energy_hartree": -1.0,
            }],
        }],
    }


def gsm_value():
    return {
        "source_stringfile": "calcs/TS0/gsm/stringfile.xyz0000",
        "parser_version": "arc-gsm-stringfile-1",
        "method": "gsm",
        "selected_source_point_index": 1,
        "points": [
            {"source_point_index": 0, "node_label": None, "geometry_xyz_text": XYZ,
             "path_coordinate_angstrom": 0.0},
            {"source_point_index": 1, "node_label": 1, "geometry_xyz_text": XYZ,
             "path_coordinate_angstrom": 0.2, "electronic_energy_hartree": -1.0},
        ],
    }


def evidence_doc():
    return {
        "schema_name": "arc-tckdb-evidence",
        "schema_version": "1.0",
        "document_id": DOC_ID,
        "output_schema_version": "1.1",
        "producer": {"name": "ARC", "version": "1.1.0", "git_commit": None},
        "records": [
            {"record_kind": "species", "label": "H2",
             "freq_hessian": {"status": "available", "value": hessian_value()}},
            {"record_kind": "transition_state", "label": "TS0",
             "irc": {"status": "available", "value": irc_value()},
             "gsm": {"status": "available", "value": gsm_value()}},
        ],
    }


def write_pair(tmp_path: Path, doc=None):
    out = tmp_path / "output"
    out.mkdir()
    (out / "tckdb_evidence.json").write_text(json.dumps(doc or evidence_doc()), encoding="utf-8")
    return EvidenceStore(tmp_path), output_doc()


def test_valid_document_acceptance_and_indexed_lookup(tmp_path):
    store, output = write_pair(tmp_path)
    lookup = store.lookup(output, "species", "H2", "freq_hessian")
    assert lookup.state == "available"
    assert lookup.value["matrix_dimension"] == 6


def test_lazy_single_read_and_cached_warning(tmp_path, monkeypatch, caplog):
    store, output = write_pair(tmp_path)
    path = tmp_path / "output" / "tckdb_evidence.json"
    path.unlink()
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"
    assert sum("ARC evidence ignored" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("output", [
    {"schema_version": "1.0", "species": [], "transition_states": []},
    {"schema_version": "1.1", "species": [], "transition_states": []},
    {"schema_version": "1.2", "species": [], "transition_states": []},
])
def test_output_without_descriptor_uses_fallback(tmp_path, output):
    assert EvidenceStore(tmp_path).lookup(output, "species", "x", "freq_hessian").state == "fallback"


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(schema_name="wrong"),
    lambda d: d.update(schema_version="2.0"),
    lambda d: d.update(document_id="f" * 32),
    lambda d: d.update(records={}),
])
def test_bad_document_uses_fallback(tmp_path, mutation):
    document = evidence_doc()
    mutation(document)
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"


@pytest.mark.parametrize("path", ["/tmp/tckdb_evidence.json", "../tckdb_evidence.json", "other.json"])
def test_unsafe_descriptor_path_rejected(tmp_path, path):
    store, output = write_pair(tmp_path)
    output["tckdb_evidence"]["path"] = path
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"


def test_duplicate_json_key_rejected(tmp_path):
    store, output = write_pair(tmp_path)
    path = tmp_path / "output" / "tckdb_evidence.json"
    path.write_text('{"schema_name":"a","schema_name":"b"}', encoding="utf-8")
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"


def test_malformed_addressable_record_isolated_from_unrelated_record(tmp_path, caplog):
    document = evidence_doc()
    document["records"][0]["unknown"] = True
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"
    assert store.lookup(output, "transition_state", "TS0", "irc").state == "available"
    assert sum("evidence record keys invalid" in record.message for record in caplog.records) == 1


def test_duplicate_record_poisons_only_that_identity(tmp_path):
    document = evidence_doc()
    document["records"].insert(1, dict(document["records"][0]))
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"
    assert store.lookup(output, "transition_state", "TS0", "gsm").state == "available"


def test_unaddressable_record_does_not_poison_valid_records(tmp_path):
    document = evidence_doc()
    document["records"].insert(0, {"record_kind": "wrong", "label": 1})
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "available"
    assert store.lookup(output, "transition_state", "TS0", "irc").state == "available"


def test_authoritative_unavailable_is_distinct_from_fallback(tmp_path):
    document = evidence_doc()
    document["records"][0]["freq_hessian"] = {
        "status": "unavailable", "reason": "parse_failed", "source_paths": ["freq.log"]
    }
    store, output = write_pair(tmp_path, document)
    lookup = store.lookup(output, "species", "H2", "freq_hessian")
    assert lookup.state == "unavailable" and lookup.reason == "parse_failed"


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(matrix_dimension=7),
    lambda v: v.update(packing="full"),
    lambda v: v["lower_triangle"].__setitem__(0, float("nan")),
])
def test_invalid_hessian_value_falls_back(tmp_path, mutate):
    document = evidence_doc()
    mutate(document["records"][0]["freq_hessian"]["value"])
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"
    assert store.lookup(output, "transition_state", "TS0", "irc").state == "available"


@pytest.mark.parametrize("mutate", [
    lambda v: v["trajectories"][0].update(declared_direction="sideways"),
    lambda v: v["trajectories"][0]["points"][0].update(source_point_index=-1),
    lambda v: v["trajectories"][0]["points"][0].update(geometry_xyz_text="bad"),
])
def test_invalid_irc_value_falls_back(tmp_path, mutate):
    document = evidence_doc()
    mutate(document["records"][1]["irc"]["value"])
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, "transition_state", "TS0", "irc").state == "fallback"


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(selected_source_point_index=9),
    lambda v: v["points"][1].update(geometry_xyz_text="1\np\nC 0 0 0\n"),
    lambda v: v["points"][1].update(path_coordinate_angstrom=-1.0),
])
def test_invalid_gsm_value_falls_back(tmp_path, mutate):
    document = evidence_doc()
    mutate(document["records"][1]["gsm"]["value"])
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, "transition_state", "TS0", "gsm").state == "fallback"


def test_output_schema_validation():
    assert validate_output_schema({"schema_version": "1.0"}) == "1.0"
    assert validate_output_schema({"schema_version": "1.2"}) == "1.2"
    with pytest.raises(ValueError, match="Unsupported"):
        validate_output_schema({"schema_version": "9.9"})


def write_parser_pair(tmp_path):
    """Match arc/parser_evidence.py's current names and producer metadata."""
    output = output_doc()
    descriptor = output.pop("tckdb_evidence")
    descriptor.update(path="parser_evidence.json", schema_name="arc-parser-evidence")
    output["parser_evidence"] = descriptor
    document = evidence_doc()
    document["schema_name"] = "arc-parser-evidence"
    document["producer"].update(arkane_version="3.3.0", arkane_git_commit=None)
    out = tmp_path / "output"
    out.mkdir()
    (out / "parser_evidence.json").write_text(json.dumps(document), encoding="utf-8")
    return EvidenceStore(tmp_path), output, document


def test_current_arc_parser_evidence_all_kinds_without_raw_logs(tmp_path):
    store, output, _ = write_parser_pair(tmp_path)
    for record_kind, label, evidence_kind in (
        ("species", "H2", "freq_hessian"),
        ("transition_state", "TS0", "irc"),
        ("transition_state", "TS0", "gsm"),
    ):
        assert store.lookup(output, record_kind, label, evidence_kind).state == "available"


@pytest.mark.parametrize("output_version,sidecar_version,state", [
    ("1.2", "1.2", "available"),   # ARC a10e8ae0: output.yml and sidecar both 1.2
    ("1.2", "1.1", "fallback"),    # a stale sidecar from the previous contract
])
def test_schema_1_2_parser_evidence_pair(tmp_path, output_version, sidecar_version, state):
    store, output, document = write_parser_pair(tmp_path)
    output["schema_version"] = output_version
    document["output_schema_version"] = sidecar_version
    (tmp_path / "output" / "parser_evidence.json").write_text(json.dumps(document))
    assert store.lookup(output, "species", "H2", "freq_hessian").state == state


@pytest.mark.parametrize("field,value", [
    ("path", "../parser_evidence.json"),
    ("schema_name", "arc-tckdb-evidence"),
    ("document_id", "f" * 32),
])
def test_current_descriptor_mismatch_never_uses_legacy_pair(tmp_path, field, value):
    store, output, _ = write_parser_pair(tmp_path)
    output["tckdb_evidence"] = output_doc()["tckdb_evidence"]
    (tmp_path / "output" / "tckdb_evidence.json").write_text(json.dumps(evidence_doc()))
    output["parser_evidence"][field] = value
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"


@pytest.mark.parametrize("field", ["arkane_version", "arkane_git_commit"])
def test_current_evidence_rejects_malformed_arkane_provenance(tmp_path, field):
    store, output, document = write_parser_pair(tmp_path)
    document["producer"][field] = {"unexpected": "mapping"}
    (tmp_path / "output" / "parser_evidence.json").write_text(json.dumps(document))
    assert store.lookup(output, "species", "H2", "freq_hessian").state == "fallback"


def test_real_current_arc_fixture_flows_into_scientific_payloads(tmp_path):
    import shutil
    from types import SimpleNamespace
    import yaml
    from tckdb_arc.adapter import TCKDBAdapter, _build_irc_result_payload, _build_path_search_result_payload
    from tckdb_schemas.fragments.calculation import HessianPayload, IRCResultPayload, PathSearchResultPayload

    fixture = Path(__file__).parent / 'fixtures' / 'current_arc'
    output = yaml.safe_load((fixture / 'output.yml').read_text())
    document = json.loads((fixture / 'parser_evidence.json').read_text())
    # ARC's golden output is input to its evidence builder. The output writer
    # adds this descriptor after the builder returns, binding the generation.
    output['parser_evidence'] = {
        key: document[key] for key in ('schema_name', 'schema_version', 'document_id')
    }
    output['parser_evidence']['path'] = 'parser_evidence.json'
    (tmp_path / 'output').mkdir()
    shutil.copyfile(fixture / 'parser_evidence.json', tmp_path / 'output' / 'parser_evidence.json')
    store = EvidenceStore(tmp_path)
    hessian = store.lookup(output, 'species', 'H2O', 'freq_hessian')
    irc = store.lookup(output, 'transition_state', 'TS0', 'irc')
    gsm = store.lookup(output, 'transition_state', 'TS0', 'gsm')
    assert [hessian.state, irc.state, gsm.state] == ['available'] * 3
    adapter = SimpleNamespace(_evidence=store)
    result = TCKDBAdapter._build_freq_hessian_payload(
        adapter, output_doc=output, species_record=output['species'][0],
        geometry_xyz_text='deliberately different conformer frame',
    )
    HessianPayload.model_validate(result)
    assert result['geometry']['xyz_text'] == hessian.value['geometry_xyz_text']
    assert result['lower_triangle_hartree_bohr2'] == hessian.value['lower_triangle']
    assert hessian.value['frame'] == 'orca_hess_atoms'
    trajectories = TCKDBAdapter._parse_irc_trajectories(adapter, output, output['transition_states'][0])
    irc_result = _build_irc_result_payload(trajectories)
    IRCResultPayload.model_validate(irc_result)
    source_points = [point for trajectory in irc.value['trajectories'] for point in trajectory['points']]
    assert len(irc_result['points']) == len(source_points)
    assert {point['direction'] for point in irc_result['points']} == {'forward', 'reverse'}
    for index, (actual, source) in enumerate(zip(irc_result['points'], source_points)):
        assert actual['point_index'] == index
        assert actual['geometry']['xyz_text'] == source['geometry_xyz_text']
        for payload_key, evidence_key in (
            ('electronic_energy_hartree', 'electronic_energy_hartree'),
            ('reaction_coordinate', 'reaction_coordinate_sqrt_amu_bohr'),
            ('max_gradient', 'max_gradient_hartree_per_bohr'),
            ('rms_gradient', 'rms_gradient_hartree_per_bohr'),
        ):
            assert actual.get(payload_key) == source.get(evidence_key)
    path = _build_path_search_result_payload(
        method='gsm', log_path=None, fallback_xyz_text=None, gsm_evidence=gsm.value,
    )
    PathSearchResultPayload.model_validate(path)
    assert len(path['points']) == len(gsm.value['points'])
    for actual, source in zip(path['points'], gsm.value['points']):
        assert actual['geometry']['xyz_text'] == source['geometry_xyz_text']
        assert actual.get('path_coordinate') == source.get('cumulative_com_superposed_displacement_angstrom')
        assert actual.get('electronic_energy_hartree') == source.get('electronic_energy_hartree')
        assert actual.get('max_gradient') == source.get('max_gradient_hartree_per_bohr')
        assert actual.get('rms_gradient') == source.get('rms_gradient_hartree_per_bohr')
        assert 'node_label' not in actual
    assert any('geometry_matched_ograd_invocation_id' in point for point in gsm.value['points'])
    assert any('electronic_energy_hartree' not in point for point in path['points'])
    assert path['selected_ts_point_index'] == gsm.value['selected_source_point_index']


@pytest.mark.parametrize('mutation', ['missing_match', 'wrong_value', 'reused_match', 'distant_match', 'not_a_list'])
def test_gsm_rejects_invalid_geometry_attachments(tmp_path, mutation):
    document = evidence_doc()
    value = document['records'][1]['gsm']['value']
    value['ograd_invocations'] = [{'invocation_id': '0000.0012', 'electronic_energy_hartree': -1.0}]
    point = value['points'][1]
    point.update(geometry_matched_ograd_invocation_id='0000.0012', geometry_match_displacement_angstrom=1e-5)
    if mutation == 'missing_match':
        del point['geometry_matched_ograd_invocation_id']
        del point['geometry_match_displacement_angstrom']
    elif mutation == 'wrong_value':
        point['electronic_energy_hartree'] = -2.0
    elif mutation == 'reused_match':
        value['points'][0].update(
            geometry_matched_ograd_invocation_id='0000.0012', geometry_match_displacement_angstrom=1e-5,
        )
    elif mutation == 'distant_match':
        point['geometry_match_displacement_angstrom'] = 0.1
    else:
        value['ograd_invocations'] = {}
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, 'transition_state', 'TS0', 'gsm').state == 'fallback'


def test_unmatched_gsm_invocations_never_become_point_energies(tmp_path):
    from tckdb_arc.adapter import _build_path_search_result_payload
    from tckdb_schemas.fragments.calculation import PathSearchResultPayload

    document = evidence_doc()
    value = document['records'][1]['gsm']['value']
    del value['points'][1]['electronic_energy_hartree']
    value['ograd_invocations'] = [{'invocation_id': '0000.0001', 'electronic_energy_hartree': -123.0}]
    store, output = write_pair(tmp_path, document)
    lookup = store.lookup(output, 'transition_state', 'TS0', 'gsm')
    assert lookup.state == 'available'
    payload = _build_path_search_result_payload(
        method='gsm', log_path=None, fallback_xyz_text=None, gsm_evidence=lookup.value,
    )
    PathSearchResultPayload.model_validate(payload)
    assert 'zero_energy_reference_hartree' not in payload
    assert all('electronic_energy_hartree' not in point for point in payload['points'])


def test_gsm_relative_energies_follow_source_indices_not_list_offsets(tmp_path):
    from tckdb_arc.adapter import _build_path_search_result_payload
    from tckdb_schemas.fragments.calculation import PathSearchResultPayload

    document = evidence_doc()
    value = document['records'][1]['gsm']['value']
    value['selected_source_point_index'] = 20
    for index, (point, energy) in enumerate(zip(value['points'], [2.0, 5.0])):
        point.pop('electronic_energy_hartree', None)
        point['source_point_index'] = 10 + index * 10
        point['stringfile_relative_energy_kcal_mol'] = energy
    store, output = write_pair(tmp_path, document)
    lookup = store.lookup(output, 'transition_state', 'TS0', 'gsm')
    assert lookup.state == 'available'
    payload = _build_path_search_result_payload(
        method='gsm', log_path=None, fallback_xyz_text=None, gsm_evidence=lookup.value,
    )
    PathSearchResultPayload.model_validate(payload)
    assert [point['relative_energy_kj_mol'] for point in payload['points']] == pytest.approx([0.0, 12.552])
    assert payload['selected_ts_point_index'] == 20


@pytest.mark.parametrize('frame', ['standard_orientation', 'gaussian_input_orientation'])
def test_hessian_rejects_unknown_or_wrong_source_frame(tmp_path, frame):
    document = evidence_doc()
    document['records'][0]['freq_hessian']['value'].update(source='parsed_hess', frame=frame)
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, 'species', 'H2', 'freq_hessian').state == 'fallback'


def test_unavailable_hessian_frame_does_not_request_legacy_parser(tmp_path):
    document = evidence_doc()
    document['records'][0]['freq_hessian'] = {'status': 'unavailable', 'reason': 'hessian_frame_unavailable'}
    store, output = write_pair(tmp_path, document)
    assert store.lookup(output, 'species', 'H2', 'freq_hessian').state == 'unavailable'


@pytest.mark.parametrize('frame_result', [None, (None, None), ({'symbols': ('H', 'H'), 'coords': ((0., 0., 0.), (0., 0., .7))}, 'standard_orientation')])
def test_raw_hessian_without_matching_frame_is_omitted(tmp_path, caplog, frame_result):
    from types import SimpleNamespace
    from unittest import mock
    from tckdb_arc.adapter import TCKDBAdapter

    log = tmp_path / 'freq.log'
    log.write_text('fixture')
    parser = SimpleNamespace(parse_cartesian_hessian_lower_triangle=lambda: [0.1] * 21)
    if frame_result is not None:
        parser.parse_cartesian_hessian_geometry = lambda: frame_result
    adapter = SimpleNamespace(_evidence=EvidenceStore(tmp_path), _resolve_local_path=lambda _: log)
    with mock.patch('tckdb_arc._arc_optional.determine_ess', return_value='gaussian'), \
         mock.patch('tckdb_arc._arc_optional.ess_factory', return_value=parser):
        result = TCKDBAdapter._build_freq_hessian_payload(
            adapter, output_doc={'schema_version': '1.0'}, species_record={'label': 'H2', 'freq_log': 'freq.log'},
            geometry_xyz_text=XYZ,
        )
    assert result is None
    assert 'matching Cartesian frame geometry unavailable' in caplog.text


@pytest.mark.parametrize('ess,frame', [('gaussian', 'gaussian_input_orientation'), ('orca', 'orca_hess_atoms')])
def test_raw_hessian_uses_parser_frame_without_conformer_geometry(tmp_path, ess, frame):
    from types import SimpleNamespace
    from unittest import mock
    from tckdb_arc.adapter import TCKDBAdapter
    from tckdb_schemas.fragments.calculation import HessianPayload

    log = tmp_path / 'freq.log'
    log.write_text('fixture')
    xyz = {'symbols': ('H', 'H'), 'coords': ((1., 2., 3.), (1., 2., 3.7))}
    parser = SimpleNamespace(
        parse_cartesian_hessian_lower_triangle=lambda: [0.1] * 21,
        parse_cartesian_hessian_geometry=lambda: (xyz, frame),
    )
    adapter = SimpleNamespace(_evidence=EvidenceStore(tmp_path), _resolve_local_path=lambda _: log)
    with mock.patch('tckdb_arc._arc_optional.determine_ess', return_value=ess), \
         mock.patch('tckdb_arc._arc_optional.ess_factory', return_value=parser):
        result = TCKDBAdapter._build_freq_hessian_payload(
            adapter, output_doc={'schema_version': '1.0'}, species_record={'label': 'H2', 'freq_log': 'freq.log'},
            geometry_xyz_text=None,
        )
    HessianPayload.model_validate(result)
    assert [float(value) for value in result['geometry']['xyz_text'].splitlines()[2].split()[1:]] == [1., 2., 3.]


def test_raw_gsm_ignores_indexed_node_outputs_and_preserves_relative_profile(caplog):
    from unittest import mock
    from tckdb_arc.adapter import _build_path_search_result_payload
    from tckdb_schemas.fragments.calculation import PathSearchResultPayload

    frames = [{'symbols': ('H', 'H'), 'coords': ((0., 0., 0.), (0., 0., z))} for z in (.7, .8, .9)]
    with mock.patch('tckdb_arc._arc_optional.parse_trajectory', return_value=frames), \
         mock.patch('tckdb_arc._arc_optional.kabsch', return_value=.1), \
         mock.patch('tckdb_arc._arc_optional.parse_gsm_stringfile_energies', return_value=[0., 2., 1.]), \
         mock.patch('tckdb_arc.adapter._read_gsm_node_outputs', side_effect=AssertionError('unsafe index mapping')):
        result = _build_path_search_result_payload(
            method='gsm', log_path='stringfile.xyz0000', fallback_xyz_text=None, node_outputs_dir='nodes',
        )
    PathSearchResultPayload.model_validate(result)
    assert len(result['points']) == 3
    assert [point['relative_energy_kj_mol'] for point in result['points']] == pytest.approx([0., 8.368, 4.184])
    assert 'zero_energy_reference_hartree' not in result
    for point in result['points']:
        assert not {'electronic_energy_hartree', 'max_gradient', 'rms_gradient'} & point.keys()
    assert 'omitted without geometry-matched parser evidence' in caplog.text
