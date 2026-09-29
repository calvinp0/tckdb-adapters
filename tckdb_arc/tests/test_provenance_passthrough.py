"""ARC's software provenance reaches TCKDB as ARC recorded it (adapter 0.6.1).

The real benzene run (fixtures/benzene_b3lyp_def2tzvp) drew three server
warnings on production because the adapter dropped data output.yml holds:

* ``software_release_version_is_composite``: ``ess_versions`` holds the raw
  banner ``Gaussian 16, Revision C.02``; the adapter now splits it exactly as
  TCKDB's shared ``SoftwareReleaseRef.normalize_composite_version`` would.
* ``missing_software_release_provenance`` on thermo and statmech: Arkane
  4.0.0 (``arkane_version``, ``arkane_git_commit``) produced both.
* ``missing_energy_correction_scheme_software``: each correction record's
  ``matched_arkane_key`` names the program whose numbers the scheme holds
  (``software='gaussian'``).

``missing_literature_provenance`` stays: ARC records no citation.
"""

import json
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest
import yaml

from tckdb_arc.adapter import (
    _arc_analysis_software_release,
    _build_applied_energy_corrections,
    _split_ess_version_banner,
)
from tckdb_schemas.fragments.refs import SoftwareReleaseRef

from test_thermo_enthalpy_declaration import _adapter

FIXTURE = Path(__file__).parent / "fixtures" / "benzene_b3lyp_def2tzvp"
ARKANE = {"name": "Arkane", "version": "4.0.0",
          "revision": "e6f47b425c9dc37ee65c072fac46c932aa265603"}
GAUSSIAN_16_C02 = {"name": "gaussian", "version": "16", "revision": "C.02"}


def _benzene(tmp_path):
    doc = yaml.safe_load((FIXTURE / "output.yml").read_text())
    (tmp_path / "output").mkdir()
    shutil.copyfile(FIXTURE / "parser_evidence.json", tmp_path / "output" / "parser_evidence.json")
    [record] = doc["species"]
    return doc, record


def _calculations(obj):
    """Every calculation dict (it has a software_release and a level of theory)."""
    if isinstance(obj, dict):
        if isinstance(obj.get("software_release"), dict) and "level_of_theory" in obj:
            yield obj
        for value in obj.values():
            yield from _calculations(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _calculations(item)


def _submit(tmp_path, mode):
    doc, record = _benzene(tmp_path)
    adapter = _adapter(tmp_path, mode=mode)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        if mode == "computed_species":
            outcome = adapter.submit_computed_species_from_output(
                output_doc=doc, species_record=record)
        else:
            outcome = adapter.submit_from_output(output_doc=doc, species_record=record)
    return json.loads(outcome.payload_path.read_text()), outcome


@pytest.mark.parametrize("mode", ["computed_species", "conformer"])
def test_benzene_calculations_carry_the_split_gaussian_release(tmp_path, mode):
    # The conftest hook has already validated the payload against the route.
    payload, _ = _submit(tmp_path, mode)
    calculations = list(_calculations(payload))
    # computed_species: coarse opt, opt, freq, sp and the screened
    # alternative conformer's opt; conformer: opt, freq, sp.
    assert len(calculations) == (5 if mode == "computed_species" else 3)
    for calc in calculations:
        assert calc["software_release"] == GAUSSIAN_16_C02, calc["type"]


def test_benzene_thermo_and_statmech_name_arkane(tmp_path):
    payload, _ = _submit(tmp_path, "computed_species")
    assert payload["thermo"]["software_release"] == ARKANE
    assert payload["statmech"]["software_release"] == ARKANE
    # Arkane is post-processing software, never a calculation's program.
    assert all(calc["software_release"]["name"] != "Arkane"
               for calc in _calculations(payload))


def test_benzene_correction_schemes_name_the_program_that_computed_them(tmp_path):
    payload, _ = _submit(tmp_path, "computed_species")
    schemes = {c["scheme"]["kind"]: c["scheme"] for c in payload["applied_energy_corrections"]}
    assert set(schemes) == {"atom_energy", "bac_petersson"}
    for scheme in schemes.values():
        # The program in Arkane's matched key; no release: ARC records none.
        assert scheme["software"] == {"name": "gaussian"}
        # The Arkane build whose table it is; never ARC, never scheme.software.
        assert scheme["workflow_tool_release"] == {
            "name": "Arkane", "version": ARKANE["version"],
            "git_commit": ARKANE["revision"]}


@pytest.mark.parametrize("name,banner,expected", [
    ("gaussian", "Gaussian 16, Revision C.02", {"version": "16", "revision": "C.02"}),
    ("gaussian", "Gaussian 09, Revision D.01", {"version": "09", "revision": "D.01"}),
    ("gaussian", "Gaussian 16", {"version": "16"}),
    ("orca", "ORCA 5.0.4", {"version": "5.0.4"}),
    ("molpro", "Molpro 2022.3", {"version": "2022.3"}),
    ("psi4", "Psi4 1.7", {"version": "1.7"}),
    # Already bare: unchanged.
    ("gaussian", "16", {"version": "16"}),
    ("orca", "5.0.4", {"version": "5.0.4"}),
    # The leading token names a different program: the server leaves both
    # fields alone (software_release_name_looks_wrong); so does the adapter.
    ("gaussian", "ORCA 6.0.0", {"version": "ORCA 6.0.0"}),
    ("qchem", "Q-Chem 5.4", {"version": "Q-Chem 5.4"}),
    # Unknown shapes are sent unchanged, never guessed at.
    ("orca", "Program Version 5.0.4", {"version": "Program Version 5.0.4"}),
    ("gaussian", "g16", {"version": "g16"}),
])
def test_banner_split_matches_the_server_normaliser(name, banner, expected):
    split = _split_ess_version_banner(name, banner)
    assert split == expected
    # The server, given the split, stores the same fields and has nothing
    # left to normalise; given the raw banner, it stores the same result.
    sent = SoftwareReleaseRef(name=name, **split)
    raw = SoftwareReleaseRef(name=name, version=banner)
    assert (sent.version, sent.revision) == (raw.version, raw.revision)
    if split != {"version": banner}:
        assert sent.version_warning() is None


def test_arkane_release_uses_only_what_arc_recorded():
    assert _arc_analysis_software_release({}) is None
    assert _arc_analysis_software_release({"arkane_version": "4.0.0"}) == {
        "name": "Arkane", "version": "4.0.0"}
    assert _arc_analysis_software_release({"arkane_git_commit": "abc123"}) == {
        "name": "Arkane", "revision": "abc123"}


def _correction(level_software=None, matched_arkane_key=None):
    level = {"method": "b3lyp", "basis": "def2tzvp"}
    if level_software is not None:
        level["software"] = level_software
    return {
        "application_role": "aec_total", "value": 1.0, "value_unit": "hartree",
        "scheme": {"kind": "atom_energy", "name": "atom_energy", "level_of_theory": level},
        "matched_arkane_key": matched_arkane_key,
    }


KEY_GAUSSIAN = "LevelOfTheory(method='b3lyp2023',basis='def2tzvp',software='gaussian')"
KEY_NO_SOFTWARE = "LevelOfTheory(method='b3lyp',basis='def2tzvp')"


@pytest.mark.parametrize("level_software,key,expected", [
    # The table's program is the Arkane key's software, not ARC's level.
    ("gaussian", KEY_GAUSSIAN, {"name": "gaussian"}),
    (None, KEY_GAUSSIAN, {"name": "gaussian"}),
    # ARC's matcher accepts a software-less key for any program: the
    # level's 'orca' says nothing about who computed Arkane's table.
    ("orca", KEY_NO_SOFTWARE, None),
    ("gaussian", None, None),        # no key recorded
    ("gaussian", "not a key", None),  # unparseable
    ("gaussian", "LevelOfTheory(method='b3lyp',software=gaussian)", None),  # unquoted
    ("gaussian", "LevelOfTheory(software='gaussian',software='orca')", None),  # ambiguous
], ids=["key_and_level", "key_only", "key_without_software", "no_key", "unparseable",
        "unquoted", "two_software_tokens"])
def test_scheme_software_comes_from_the_matched_arkane_key(level_software, key, expected):
    warnings = []
    [correction] = _build_applied_energy_corrections(
        [_correction(level_software, key)], warnings=warnings)
    assert correction["scheme"].get("software") == expected
    assert "matched_arkane_key" not in correction
    assert warnings == []


def test_scheme_software_disagreement_is_omitted_and_reported():
    warnings = []
    [correction] = _build_applied_energy_corrections(
        [_correction("orca", KEY_GAUSSIAN)], warnings=warnings)
    assert "software" not in correction["scheme"]
    [warning] = warnings
    assert warning["code"] == "energy_correction_scheme_software_conflict"
    assert warning["field"] == "applied_energy_corrections.scheme.software"
    assert warning["context"] == {
        "source": "tckdb_arc_self_check", "action": "scheme_software_omitted",
        "scheme_kind": "atom_energy", "arkane_key_software": "gaussian",
        "level_software": "orca"}


def test_benzene_scheme_software_disagreement_reaches_the_sidecar(tmp_path):
    doc, record = _benzene(tmp_path)
    for correction in record["energy_corrections"]:
        correction["level_of_theory"] = dict(correction["level_of_theory"], software="orca")
    adapter = _adapter(tmp_path, mode="computed_species")
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = adapter.submit_computed_species_from_output(output_doc=doc, species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    assert all("software" not in c["scheme"] for c in payload["applied_energy_corrections"])
    codes = [w["code"] for w in outcome.warnings]
    assert codes.count("energy_correction_scheme_software_conflict") == 2
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings
