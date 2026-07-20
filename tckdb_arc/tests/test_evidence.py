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
    with pytest.raises(ValueError, match="Unsupported"):
        validate_output_schema({"schema_version": "9.9"})
