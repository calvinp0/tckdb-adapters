"""G4 / G4MP2 on Gaussian 16 Revision A.03: the energy ARC read is the 298 K value, so it is withheld.

Gaussian 16 Rev A.03's summary table shifts the ``G4(0 K)`` / ``G4MP2(0 K)`` label by one entry; ARC's
parser (like Arkane's ``load_energy``) reads the number after the label, so ``sp_energy_hartree`` is wrong
by 9-11 kJ/mol. Stopgap until ARC exports E0 from the archive (ARC_TCKDB_EXPORT_BRIEF.md, Bug 8): the record's
sp energy, its E0-derived thermo content (H298, NASA, point H and G) and the statmech energy links are not
sent, S298 and Cp (from statmech, not E0) are kept, and a warning names the cause. Only output 1.3 records
carry ``ess_versions.composite``, so an earlier document is never guarded.
"""

import copy

import pytest

from tckdb_arc.adapter import _g4_loader_energy_unreliable, _withhold_unreliable_composite_energies

from test_arc_schema_1_3_thermo import ROUTES, _doc, _species, build, build_reaction

CODE = "g4_energy_loader_shifted_label_gaussian16_a03"
A03 = "Gaussian 16, Revision A.03"


def _g4_doc(method="g4", banner=A03, *, header_only=False):
    doc = _doc()
    record = _species(doc, "sBuOH")
    # A composite run: the composite job's log is the opt log (no separate opt job) and the geometry's level.
    record["composite_log"] = record["opt_log"]
    record["levels"]["opt"] = None
    record["ess_software"] = {**(record.get("ess_software") or {}), "composite": "gaussian"}
    record["ess_versions"] = {**(record.get("ess_versions") or {}), "composite": banner}
    if header_only:
        doc["composite_method"] = {"method": method, "method_type": "composite", "software": "gaussian"}
    else:
        record["levels"]["composite"] = {"method": method}
    return doc


def test_the_fixture_is_the_unreliable_case():
    doc = _g4_doc()
    assert _g4_loader_energy_unreliable(doc, _species(doc, "sBuOH")) == ("g4", A03)


@pytest.mark.parametrize("method, key", [("g4", "g4"), ("G4", "g4"), ("g4mp2", "g4mp2"), ("g4(mp2)", "g4mp2")])
def test_g4_and_g4mp2_spellings(method, key):
    doc = _g4_doc(method)
    assert _g4_loader_energy_unreliable(doc, _species(doc, "sBuOH"))[0] == key


def test_the_header_composite_method_is_read_when_the_record_states_no_composite_level():
    doc = _g4_doc("g4mp2", header_only=True)
    assert _g4_loader_energy_unreliable(doc, _species(doc, "sBuOH")) == ("g4mp2", A03)


@pytest.mark.parametrize("method, banner", [
    ("cbs-qb3", A03),                                  # a composite whose label is not shifted
    ("g3", A03),
    ("g4", "Gaussian 16, Revision C.01"),              # another revision
    ("g4", "Gaussian 09, Revision A.03"),              # another program release
    ("g4", None),                                      # banner not stated: nothing inferred
    ("g4", "ORCA 5.0.4"),
])
def test_other_composites_revisions_and_unstated_banners_are_untouched(method, banner):
    doc = _g4_doc(method, banner or A03)
    record = _species(doc, "sBuOH")
    if banner is None:
        record["ess_versions"].pop("composite")
    assert _g4_loader_energy_unreliable(doc, record) is None
    assert _withhold_unreliable_composite_energies(doc, record, []) is record


def test_a_non_composite_run_is_untouched():
    doc = _g4_doc()
    record = _species(doc, "sBuOH")
    record["composite_log"] = None
    assert _g4_loader_energy_unreliable(doc, record) is None


def _sp_energies(payload):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "sp":
                out.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(payload)
    return out


C01 = "Gaussian 16, Revision C.01"


@pytest.mark.parametrize("route", ROUTES)
def test_the_records_energies_are_not_sent_and_the_cause_is_named(tmp_path, route):
    # The same record with an unshifted revision: what an unaffected composite run sends.
    ok = build(tmp_path / "ok", route, _g4_doc(banner=C01), "sBuOH")
    assert ok.calcs("sp")
    if route != "conformer":                               # the conformer upload carries no thermo
        assert ok.thermo is not None
    built = build(tmp_path / "g4", route, _g4_doc(), "sBuOH")
    if route != "conformer":
        # E0-derived content is gone, the statmech-derived content is kept
        assert built.thermo is not None
        assert not {"h298_kj_mol", "nasa", "enthalpy_reference_kind"} & set(built.thermo)
        assert built.thermo["s298_j_mol_k"] == ok.thermo["s298_j_mol_k"]
        assert all("h_kj_mol" not in p and "g_kj_mol" not in p for p in built.thermo.get("points", ()))
        assert any("cp_j_mol_k" in p for p in built.thermo.get("points", ()))
    assert built.calcs("sp") == []
    roles = {link["role"] for link in (built.statmech or {}).get("source_calculations", [])}
    assert roles and not roles & {"sp", "composite"}
    assert "composite" in {link["role"] for link in (ok.statmech or {}).get("source_calculations", [])}
    warning = next(w for w in built.warnings if w["code"] == CODE)
    assert "G4" in warning["message"] and "A.03" in warning["message"]
    assert "H298" in warning["message"] and "S298, Cp" in warning["message"]
    assert warning["context"] == {"source": "tckdb_arc_self_check", "action": "record_energies_withheld",
                                  "label": "sBuOH", "method": "g4", "ess_version": A03}


@pytest.mark.parametrize("route", ROUTES)
def test_an_unaffected_banner_warns_of_nothing(tmp_path, route):
    other = build(tmp_path / "b", route, _g4_doc("g4", C01), "sBuOH")
    assert CODE not in other.warning_codes()


def test_a_reaction_with_an_affected_participant_loses_its_energies_and_kinetics(tmp_path):
    payload, warnings = build_reaction(tmp_path, _g4_doc())
    codes = [w["code"] for w in warnings]
    assert codes.count(CODE) == 2                          # the record, then the kinetics fitted from it
    assert {w["field"] for w in warnings if w["code"] == CODE} == {"sBuOH.energies", "kinetics"}
    assert not payload.get("kinetics")
    sbuoh = payload["species"][0]
    thermo = sbuoh.get("thermo")
    assert thermo is not None and "h298_kj_mol" not in thermo and "nasa" not in thermo
    assert "s298_j_mol_k" in thermo
    assert not _sp_energies(sbuoh)


def test_the_input_record_is_not_mutated():
    doc = _g4_doc()
    record = _species(doc, "sBuOH")
    before = copy.deepcopy(record)
    out = _withhold_unreliable_composite_energies(doc, record, [])
    assert out is not record and record == before
    assert out["sp_energy_hartree"] is None
    assert "_tckdb_thermo_enthalpy_withheld" in out["thermo"] and "_tckdb_thermo_enthalpy_withheld" not in record["thermo"]


def test_a_pre_1_3_document_carries_no_composite_banner_so_the_guard_cannot_fire():
    doc = _g4_doc()
    record = _species(doc, "sBuOH")
    record.pop("ess_versions")                              # pre-1.3 records have no per-job banners
    assert _g4_loader_energy_unreliable(doc, record) is None
    assert _withhold_unreliable_composite_energies(doc, record, []) is record
