"""Upload the offline corpora to a live, isolated TCKDB and read back what was stored.

Opt-in: every test except the guard ones takes ``live_tckdb`` and is skipped
unless ``TCKDB_INTEGRATION_URL`` names an isolated loopback backend (see
``conftest.py`` and ``docs/contract/INTEGRATION_GATE.md``). Uploads go through
the adapter's real transport (``tckdb-client``), so idempotency keys,
sidecars and warning plumbing are exercised as in an ARC run. Project labels
are fixed, so re-running against the same database replays rather than
duplicates, and every check tolerates a first upload that is itself a replay.

Checks read the persisted rows back through the id-addressed REST API, after
waiting for the upload's commit (TCKDB commits after answering; T4).

Known adapter gaps are pinned as strict xfails that accept only an
``AssertionError`` from the pinned check. Setup failures raise
``RuntimeError`` so they fail the test instead of satisfying the xfail.
"""

import hashlib
import json
import re
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tckdb_arc.sweep import run_upload_sweep

from _live import (
    CURRENT_ARC,
    GOLDEN,
    assert_integration_backend,
    assert_loopback_url,
    attach_log,
    make_adapter,
    make_config,
    materialize,
    read_sidecars,
    verify_isolated_backend,
)


# ---------------------------------------------------------------------------
# Target guards (offline; run in the default suite)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:58010/api/v1",
    "http://localhost:58010/api/v1",
    "http://[::1]:58010/api/v1",
])
def test_loopback_urls_are_accepted(url):
    assert assert_loopback_url(url) == url


@pytest.mark.parametrize("url", [
    "https://tckdb.example.org/api/v1",
    "http://192.168.1.20:58010/api/v1",
    "http://127.0.0.1.example.org/api/v1",
    "",
    None,
])
def test_non_loopback_urls_are_refused(url):
    with pytest.raises(ValueError, match="refusing TCKDB integration target"):
        assert_loopback_url(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8010/api/v1",
    "http://localhost:8010/api/v1",
    "http://[::1]:8010",
])
def test_port_8010_is_refused_even_on_loopback(url):
    with pytest.raises(ValueError, match="port 8010"):
        assert_loopback_url(url)


def _status(bucket):
    return {"status": "ok", "components": {"artifact_storage": {"bucket": bucket}}}


def test_integration_bucket_is_accepted():
    assert assert_integration_backend(_status("tckdb-integ-artifacts")) == "tckdb-integ-artifacts"


@pytest.mark.parametrize("status", [
    _status("tckdb-artifacts"),
    _status("tckdb-artifacts-prod"),
    _status(None),
    {"status": "ok", "components": {}},
    {},
    None,
])
def test_non_integration_backend_is_refused(status):
    with pytest.raises(ValueError, match="not an isolated integration bucket"):
        assert_integration_backend(status)


class _FakeTCKDB(ThreadingHTTPServer):
    """A loopback stand-in answering /status and /readyz, recording request headers."""

    def __init__(self, bucket):
        self.bucket = bucket
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):  # noqa: N805 - http.server's naming
                self.requests.append((handler.path, {k.lower(): v for k, v in handler.headers.items()}))
                body = {"/api/v1/status": _status(self.bucket),
                        "/api/v1/readyz": {"status": "ready"}}.get(handler.path)
                payload = json.dumps(body or {"detail": "not found"}).encode()
                handler.send_response(200 if body else 404)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)

            def log_message(handler, *args):  # noqa: N805
                pass

        super().__init__(("127.0.0.1", 0), Handler)

    def __enter__(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/api/v1"


@pytest.mark.parametrize("bucket,accepted", [
    ("tckdb-integ-artifacts", True),
    ("tckdb-artifacts", False),
])
def test_backend_is_verified_without_sending_the_api_key(monkeypatch, bucket, accepted):
    monkeypatch.setenv("TCKDB_INTEGRATION_API_KEY", "tck_must_not_leave_the_process")
    monkeypatch.setenv("TCKDB_API_KEY", "tck_must_not_leave_the_process")
    with _FakeTCKDB(bucket) as server:
        assert server.server_address[1] != 8010
        if accepted:
            assert verify_isolated_backend(server.url, timeout=5) == server.url
        else:
            with pytest.raises(ValueError, match="not an isolated integration bucket"):
                verify_isolated_backend(server.url, timeout=5)
    paths = [path for path, _ in server.requests]
    assert paths == (["/api/v1/status", "/api/v1/readyz"] if accepted else ["/api/v1/status"])
    for _, headers in server.requests:
        assert "x-api-key" not in headers
        assert "tck_must_not_leave_the_process" not in json.dumps(headers)


def test_adapter_config_refuses_non_loopback_target():
    with pytest.raises(ValueError, match="refusing"):
        make_config("https://tckdb.example.org/api/v1", "golden", "computed_species")
    with pytest.raises(ValueError, match="port 8010"):
        make_config("http://127.0.0.1:8010/api/v1", "golden", "computed_species")


# ---------------------------------------------------------------------------
# Helpers (setup failures raise RuntimeError, never AssertionError)
# ---------------------------------------------------------------------------


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _uploaded(live, outcome):
    """The response of a successful upload, once its rows are readable."""
    _require(outcome is not None, "adapter returned no outcome")
    _require(outcome.status == "uploaded", f"upload {outcome.status}: {outcome.error}")
    live.await_commit(outcome.response)
    return outcome.response


def _submit_species(live, tmp_path, name, mode="computed_species"):
    project, doc = materialize(tmp_path, name)
    adapter = make_adapter(live.url, project, name, mode)
    submit = (adapter.submit_computed_species_from_output if mode == "computed_species"
              else adapter.submit_from_output)
    return {
        record["label"]: submit(output_doc=doc, species_record=record)
        for record in doc["species"] if record.get("converged")
    }


def _species_calcs(response):
    conformer = response["conformers"][0]
    calcs = [conformer["primary_calculation"], *conformer["additional_calculations"]]
    return {calc["type"]: calc["calculation_id"] for calc in calcs}


def _codes(warnings):
    return {w.get("code") for w in warnings}


def _formula(xyz_text):
    return Counter(line.split()[0].strip() for line in xyz_text.splitlines()[2:] if line.strip())


def _species_formula(formula):
    counts = Counter()
    for element, number in re.findall(r"([A-Z][a-z]?)(\d*)", formula):
        counts[element] += int(number or 1)
    return counts


def _evidence_value(path, label, kind):
    document = json.loads(Path(path).read_text())
    return next(r for r in document["records"] if r["label"] == label)[kind]["value"]


def _ts_validation(live, entry_id):
    record = live.get(f"/scientific/transition-state-entries/{entry_id}",
                      include="validation_evidence")["record"]
    return record["validation"]["irc"], record["validation_evidence"]


# ---------------------------------------------------------------------------
# computed_species
# ---------------------------------------------------------------------------


def test_golden_species_calculations_thermo_and_hessian(live_tckdb, tmp_path):
    project, doc = materialize(tmp_path, "golden")
    adapter = make_adapter(live_tckdb.url, project, "golden", "computed_species")
    outcomes = {
        record["label"]: adapter.submit_computed_species_from_output(output_doc=doc, species_record=record)
        for record in doc["species"] if record.get("converged")
    }
    # The golden H is a single atom: ARC never optimises it, and since
    # tckdb-schemas 0.59 (TCKDB#610) the atom's primary calculation is its own sp:
    # no placeholder opt and no warning (A6).
    assert not [c for c in _codes(outcomes["H"].warnings) if "placeholder" in c]
    atom_calcs = _species_calcs(_uploaded(live_tckdb, outcomes["H"]))
    assert set(atom_calcs) == {"sp"}
    response = _uploaded(live_tckdb, outcomes["H2"])
    calcs = _species_calcs(response)
    assert set(calcs) == {"opt", "freq", "sp"}
    for calc_type, calc_id in calcs.items():
        stored = live_tckdb.get(f"/calculations/{calc_id}")
        assert stored["type"] == calc_type
        assert stored["species_entry_id"] == response["species_entry_id"]
        assert stored["transition_state_entry_id"] is None

    # Golden output.yml predates the 1.2 atom-correction flags, and H2 is too
    # light for the magnitude guard, so its formation enthalpy cannot be
    # verified: H298, NASA and point H/G are withheld, S and Cp are kept.
    unverifiable = "enthalpy_formation_unverifiable_light_species"
    assert unverifiable in _codes(outcomes["H2"].warnings)
    assert unverifiable in _codes(json.loads(outcomes["H2"].sidecar_path.read_text())["warnings"])
    source = next(r for r in doc["species"] if r["label"] == "H2")["thermo"]
    thermo = live_tckdb.get(f"/thermo/{response['thermo']['thermo_id']}")
    assert thermo["species_entry_id"] == response["species_entry_id"]
    assert thermo["h298_kj_mol"] is None
    assert thermo["enthalpy_reference_kind"] is None
    assert thermo["nasa"] is None
    assert thermo["s298_j_mol_k"] == pytest.approx(source["s298_j_mol_k"])
    assert (thermo["tmin_k"], thermo["tmax_k"]) == (source["tmin_k"], source["tmax_k"])
    # The golden output.yml records no standard_state_pressure_pa, so the
    # adapter omits reference_pressure_bar (never defaulting RMG's 1 atm)
    # and TCKDB stores the entropy's pressure as not stated.
    assert "standard_state_pressure_pa" not in source
    assert "reference_pressure_bar" in thermo
    assert thermo["reference_pressure_bar"] is None
    unstated = "thermo_reference_pressure_not_stated"
    assert unstated in _codes(outcomes["H2"].warnings)
    assert unstated in _codes(json.loads(outcomes["H2"].sidecar_path.read_text())["warnings"])
    assert [(p["temperature_k"], p["cp_j_mol_k"], p["s_j_mol_k"], p["h_kj_mol"], p["g_kj_mol"])
            for p in thermo["points"]] == [
        (p["temperature_k"], p["cp_j_mol_k"], p["s_j_mol_k"], None, None)
        for p in source["thermo_points"]
    ]
    assert {(s["role"], s["calculation_id"]) for s in thermo["source_calculations"]} == {
        (calc_type, calc_id) for calc_type, calc_id in calcs.items()
    }

    expected = _evidence_value(GOLDEN / "tckdb_evidence.json", "H2", "freq_hessian")
    hessian = live_tckdb.get(f"/calculations/{calcs['freq']}/hessian")
    assert hessian["natoms"] == expected["atom_count"] == 2
    assert hessian["lower_triangle_hartree_bohr2"] == expected["lower_triangle"]

    assert _uploaded(live_tckdb, outcomes["H"])["thermo"] is None


ENTHALPY_KEPT = {"CH4"}
ENTHALPY_STRIPPED = {
    "CH4_standin": "enthalpy_atom_corrections_level_mismatch",
    "CH4_uncorrected": "enthalpy_atom_corrections_not_applied",
    "CH4_yml": "enthalpy_not_formation_magnitude",
    "H": "enthalpy_atom_corrections_not_applied",
    "H2": "enthalpy_atom_corrections_not_applied",
}


def test_arc_1_2_formation_enthalpy_kept_or_stripped_to_s_cp(live_tckdb, tmp_path):
    outcomes = _submit_species(live_tckdb, tmp_path, "arc_1_2")
    assert set(outcomes) == ENTHALPY_KEPT | set(ENTHALPY_STRIPPED)
    for label, outcome in outcomes.items():
        thermo = live_tckdb.get(f"/thermo/{_uploaded(live_tckdb, outcome)['thermo']['thermo_id']}")
        # ARC 1.2 records standard_state_pressure_pa = 101325 Pa: stated, so sent.
        assert thermo["reference_pressure_bar"] == pytest.approx(1.01325), label
        assert "thermo_reference_pressure_not_stated" not in _codes(outcome.warnings), label
        assert thermo["s298_j_mol_k"] is not None, label
        assert all(p["s_j_mol_k"] is not None and p["cp_j_mol_k"] is not None
                   for p in thermo["points"]), label
        if label in ENTHALPY_KEPT:
            assert thermo["enthalpy_reference_kind"] == "formation_298k"
            assert thermo["h298_kj_mol"] == pytest.approx(-74.6)
            assert thermo["nasa"] is not None
            continue
        assert ENTHALPY_STRIPPED[label] in _codes(outcome.warnings), label
        assert thermo["h298_kj_mol"] is None, label
        assert thermo["enthalpy_reference_kind"] is None, label
        assert thermo["nasa"] is None, label
        assert all(p["h_kj_mol"] is None and p["g_kj_mol"] is None
                   for p in thermo["points"]), label


def test_current_arc_parser_evidence_hessian_is_stored_verbatim(live_tckdb, tmp_path):
    response = _uploaded(live_tckdb, _submit_species(live_tckdb, tmp_path, "current_arc_h2o")["H2O"])
    stored = live_tckdb.get(f"/calculations/{_species_calcs(response)['freq']}/hessian")
    expected = _evidence_value(CURRENT_ARC / "parser_evidence.json", "H2O", "freq_hessian")
    assert stored["lower_triangle_hartree_bohr2"] == expected["lower_triangle"]
    assert stored["natoms"] == expected["atom_count"]
    assert stored["source"] == expected["source"]
    assert stored["parser_version"] == expected["parser_version"]


def test_applied_energy_corrections_link_species_and_sp(live_tckdb, tmp_path):
    response = _uploaded(live_tckdb, _submit_species(live_tckdb, tmp_path, "arc_1_2_corrections")["CH4"])
    sp_id = _species_calcs(response)["sp"]
    rows = [
        row for row in live_tckdb.get(
            "/applied-energy-corrections",
            target_species_entry_id=response["species_entry_id"], limit=200,
        )["items"]
        if row["source_calculation_id"] == sp_id
    ]
    by_role = {row["application_role"]: row for row in rows}
    assert set(by_role) == {"aec_total", "bac_total"}
    assert (by_role["aec_total"]["value"], by_role["aec_total"]["value_unit"]) == (-0.0234, "hartree")
    assert (by_role["bac_total"]["value"], by_role["bac_total"]["value_unit"]) == (-0.694, "kcal_mol")
    assert {(c["key"], c["multiplicity"]) for c in by_role["aec_total"]["components"]} == {("C", 1), ("H", 4)}
    assert [(c["key"], c["multiplicity"]) for c in by_role["bac_total"]["components"]] == [("C-H", 4)]


# ---------------------------------------------------------------------------
# computed_reaction / computed_ts
# ---------------------------------------------------------------------------


def _submit_reaction(live, tmp_path, name):
    project, doc = materialize(tmp_path, name)
    adapter = make_adapter(live.url, project, name, "computed_reaction")
    outcome = adapter.submit_computed_reaction_from_output(
        output_doc=doc, reaction_record=doc["reactions"][0])
    return doc, outcome, _uploaded(live, outcome)


@pytest.mark.parametrize("name", ["golden_kinetics", "synthetic_reaction"])
def test_reaction_kinetics_participants_and_ts(live_tckdb, tmp_path, name):
    doc, outcome, response = _submit_reaction(live_tckdb, tmp_path, name)
    reaction = doc["reactions"][0]
    keys = response["calculation_keys"]

    # Kinetics: ARC's A is stored as sent, with RMG's T0 as ``t0_k`` (tckdb-schemas 0.63),
    # so k(T) = a (T/t0_k)**n exp(-Ea/RT) is ARC's own.
    source = reaction["kinetics"]
    kinetics = live_tckdb.get(f"/kinetics/{response['kinetics_ids'][0]}")
    assert kinetics["reaction_entry_id"] == response["reaction_entry_id"]
    assert kinetics["a"] == pytest.approx(source["A"], rel=1e-12)
    assert kinetics.get("t0_k", 1.0) == pytest.approx(source["T0_k"])
    assert kinetics["a_units"] == "cm3_mol_s"
    assert kinetics["n"] == pytest.approx(source["n"])
    assert kinetics["ea_kj_mol"] == pytest.approx(source["Ea"])
    for temperature in (300.0, 1000.0):
        assert kinetics["a"] * (temperature / kinetics.get("t0_k", 1.0)) ** kinetics["n"] == pytest.approx(
            source["A"] * (temperature / source["T0_k"]) ** source["n"], rel=1e-12)
    roles = Counter(s["role"] for s in kinetics["source_calculations"])
    assert roles["reactant_energy"] == len(reaction["reactant_labels"])
    assert roles["product_energy"] == len(reaction["product_labels"])
    assert roles["ts_energy"] == 1 and roles["freq"] == 1
    ts_calc_ids = {v for k, v in keys.items() if k.startswith("ts_")}
    for src in kinetics["source_calculations"]:
        calc = live_tckdb.get(f"/calculations/{src['calculation_id']}")
        if src["role"] in ("ts_energy", "freq", "irc"):
            assert src["calculation_id"] in ts_calc_ids
            assert calc["transition_state_entry_id"] == response["transition_state_entry_id"]
        else:
            assert calc["species_entry_id"] in response["species_entry_ids"]

    # The saddle point is made of exactly the reactants' atoms, and the products'.
    full = live_tckdb.get(f"/scientific/reaction-entries/{response['reaction_entry_id']}/full")
    reactants = sum((_species_formula(s["formula"]) for s in full["species"]["reactants"]), Counter())
    products = sum((_species_formula(s["formula"]) for s in full["species"]["products"]), Counter())
    ts_geometry = live_tckdb.get(f"/calculations/{keys['ts_opt']}/output-geometries")[0]["geometry"]
    assert _formula(ts_geometry["xyz_text"]) == reactants == products
    assert [s["smiles"] for s in full["species"]["reactants"]] == [
        next(r["smiles"] for r in doc["species"] if r["label"] == label)
        for label in reaction["reactant_labels"]
    ]

    if name == "golden_kinetics":
        assert kinetics["software_release_id"] is not None  # Arkane, from arkane_git_commit
        ts_hessian = _evidence_value(GOLDEN / "tckdb_evidence.json", "TS0", "freq_hessian")
        stored = live_tckdb.get(f"/calculations/{keys['ts_freq']}/hessian")
        assert stored["lower_triangle_hartree_bohr2"] == ts_hessian["lower_triangle"]
        irc = live_tckdb.get(f"/calculations/{keys['ts_irc']}/irc-result")
        assert irc["has_forward"] and irc["has_reverse"] and irc["point_count"] == len(irc["points"])
        # TS0's guess is an xtb-gsm path search and ARC exports no GSM level, so
        # the adapter files no path-search calculation (never one at opt_level)
        # and says so.
        assert "ts_guess" not in keys
        assert "ts_guess_level_not_stated" in _codes(outcome.warnings)


def test_standalone_ts_upload_carries_its_calculations(live_tckdb, tmp_path):
    project, doc = materialize(tmp_path, "golden_kinetics")
    adapter = make_adapter(live_tckdb.url, project, "golden_kinetics", "computed_ts")
    response = _uploaded(live_tckdb, adapter.submit_computed_ts_from_output(
        output_doc=doc, ts_record=doc["transition_states"][0], reaction_record=doc["reactions"][0]))
    entry = live_tckdb.get(f"/transition-states/entries/{response['id']}")
    assert (entry["charge"], entry["multiplicity"]) == (0, 2)
    calcs = live_tckdb.get("/calculations", transition_state_entry_id=response["id"], limit=50)["items"]
    assert Counter(c["type"] for c in calcs) == Counter(["opt", "freq", "sp", "irc"])


def test_irc_jobs_completing_is_not_deposited_as_validation(live_tckdb, tmp_path):
    # irc_converged only says the IRC jobs finished; with no ts_checks verdict
    # the adapter correctly deposits no evidence and the server says so.
    doc, outcome, response = _submit_reaction(live_tckdb, tmp_path, "golden_kinetics")
    ts = doc["transition_states"][0]
    assert ts["irc_converged"] is True and ts.get("ts_checks") is None
    assert "transition_state_missing_irc_evidence" in _codes(outcome.warnings)
    assert _ts_validation(live_tckdb, response["transition_state_entry_id"]) == ("absent", [])


def test_passed_irc_verdict_is_deposited_as_validation_evidence(live_tckdb, tmp_path):
    _, outcome, response = _submit_reaction(live_tckdb, tmp_path, "golden_irc_passed")
    irc, evidence = _ts_validation(live_tckdb, response["transition_state_entry_id"])
    assert (irc, [e["passed"] for e in evidence]) == ("present", [True])
    assert "transition_state_missing_irc_evidence" not in _codes(outcome.warnings)


def test_failed_irc_verdict_is_deposited_as_failed_evidence(live_tckdb, tmp_path):
    _, _, response = _submit_reaction(live_tckdb, tmp_path, "golden_irc_failed")
    irc, evidence = _ts_validation(live_tckdb, response["transition_state_entry_id"])
    assert (irc, [e["passed"] for e in evidence]) == ("failed", [False])


def _evidence_by_kind(live, entry_id):
    record = live.get(f"/scientific/transition-state-entries/{entry_id}",
                      include="validation_evidence")["record"]
    evidence = record["validation_evidence"]
    kinds = [e["kind"] for e in evidence]
    assert len(kinds) == len(set(kinds)), kinds  # at most one record per kind
    return {e["kind"]: e for e in evidence}


def _calculation(live, ref_or_id):
    """The scientific read of one calculation: the public ref, type and owner kind the evidence cites."""
    record = live.get(f"/scientific/calculations/{ref_or_id}")["record"]
    return record["calculation"]["calculation_ref"], record["calculation"]["type"], record["owner"]["kind"]


def _calculation_ref(live, calculation_id):
    return _calculation(live, calculation_id)[0]


def test_reaction_bundle_deposits_imaginary_mode_and_energy_ordering_evidence(live_tckdb, tmp_path):
    # The server-only rules (stored-row ownership of each compared energy, the stored-frequency
    # cross-check, electronic energy from an sp/opt, one level per energy kind) run only here.
    doc, outcome, response = _submit_reaction(live_tckdb, tmp_path, "golden_ts_evidence")
    ts = doc["transition_states"][0]
    keys = response["calculation_keys"]
    evidence = _evidence_by_kind(live_tckdb, response["transition_state_entry_id"])
    assert set(evidence) == {"irc", "imaginary_mode", "energy_ordering"}
    assert all(e["passed"] is True for e in evidence.values())
    assert "transition_state_missing_irc_evidence" not in _codes(outcome.warnings)
    assert "transition_state_energy_ordering_mixed_levels" not in _codes(outcome.warnings)
    assert "ts_energy_ordering_evidence_not_sent" not in _codes(outcome.warnings)
    assert "ts_imaginary_mode_evidence_not_sent" not in _codes(outcome.warnings)

    mode = evidence["imaginary_mode"]
    assert mode["imaginary_frequency_count"] == ts["freq_n_imag"] == 1
    assert mode["imaginary_frequency_cm1"] == pytest.approx(ts["imag_freq_cm1"])
    assert mode["mode_displacement_agrees"] is None  # NMD unassessed: not a verdict
    assert mode["reconstruction_calculation_ref"] == _calculation_ref(live_tckdb, keys["ts_freq"])

    ordering = evidence["energy_ordering"]
    assert ordering["reconstruction_calculation_ref"] is None
    species = {s["label"]: s for s in doc["species"]}
    reaction = doc["reactions"][0]
    expected = [("ts", ts["sp_energy_hartree"])]
    for side, labels in (("reactant", reaction["reactant_labels"]), ("product", reaction["product_labels"])):
        for position, label in enumerate(labels, start=1):
            expected.append((f"{side}:{position}", species[label]["sp_energy_hartree"]))
    stored = {(e["participant"], e["energy_kind"]): e for e in ordering["compared_energies"]}
    assert set(stored) == {(participant, "electronic") for participant, _ in expected}
    for participant, energy in expected:
        assert stored[(participant, "electronic")]["energy_hartree"] == pytest.approx(energy)
    # Each energy is cited to its own participant's sp calculation, as stored.
    assert stored[("ts", "electronic")]["source_calculation_ref"] == _calculation_ref(live_tckdb, keys["ts_sp"])
    sp_refs = {e["source_calculation_ref"] for p, e in stored.items() if p[0] != "ts"}
    assert len(sp_refs) == 2  # H2's and H's sp, each cited from both sides
    for ref in sp_refs:
        assert _calculation(live_tckdb, ref)[1:] == ("sp", "species_entry")


def test_standalone_ts_upload_deposits_imaginary_mode_evidence(live_tckdb, tmp_path):
    project, doc = materialize(tmp_path, "golden_ts_evidence")
    adapter = make_adapter(live_tckdb.url, project, "golden_ts_evidence", "computed_ts")
    outcome = adapter.submit_computed_ts_from_output(
        output_doc=doc, ts_record=doc["transition_states"][0], reaction_record=doc["reactions"][0])
    response = _uploaded(live_tckdb, outcome)
    ts = doc["transition_states"][0]
    evidence = _evidence_by_kind(live_tckdb, response["id"])
    # The standalone route refuses energy_ordering; the adapter never sends it there.
    assert set(evidence) == {"irc", "imaginary_mode"}
    mode = evidence["imaginary_mode"]
    assert mode["passed"] is True
    assert mode["imaginary_frequency_count"] == 1
    assert mode["imaginary_frequency_cm1"] == pytest.approx(ts["imag_freq_cm1"])
    freq = live_tckdb.get("/calculations", transition_state_entry_id=response["id"], type="freq", limit=5)["items"]
    assert [mode["reconstruction_calculation_ref"]] == [_calculation_ref(live_tckdb, c["id"]) for c in freq]
    assert "ts_energy_ordering_evidence_not_sent" not in _codes(outcome.warnings)


# ---------------------------------------------------------------------------
# reaction atom map (ARC 1.3 ts_atom_map -> TCKDB atom_map, adapter 0.9.0)
# ---------------------------------------------------------------------------

# Warnings that would mean the map did not reach TCKDB, or reached it contradicted. The first three are
# the adapter's own (it omits the map and says why); the rest are the server's: the absence warning, the
# partial-map warnings and the IRC cross-check's refusal code.
_ATOM_MAP_WARNINGS = {
    "reaction_atom_map_absent",
    "reaction_ts_atom_map_not_sent",
    "reaction_species_labels_contradicted",
    "reaction_atom_map_ts_order_not_stated",
    "reaction_atom_map_participants_incomplete",
    "reaction_atom_map_atoms_incomplete",
    "atom_map_contradicts_irc_mapping",
}

# (corpus, reaction index): the real ARC sample, then the derived fixture's two reactions.
ATOM_MAP_CASES = [
    pytest.param("ts_atom_map_sample", 0, id="real_sample_nC3H7_iC3H7"),
    pytest.param("ts_atom_map_derived", 0, id="derived_OH_CH4_ts_order_unlike_reactants"),
    pytest.param("ts_atom_map_derived", 1, id="derived_CH3_CH3_repeated_reactant"),
]


def _atom_count(xyz):
    return len([line for line in xyz.splitlines() if line.strip()])


def _stored_atom_map(live, reaction_entry_id):
    """The one stored atom map of a reaction entry, with its participants (``include=atom_map``)."""
    full = live.get(f"/scientific/reaction-entries/{reaction_entry_id}/full", include="species,atom_map")
    assert len(full["atom_map"]) == 1, full["atom_map"]
    badges = full["reaction_entry"]["atom_maps"]
    assert len(badges) == 1 and badges[0]["transition_state_entry_ref"] == full["atom_map"][0]["transition_state_entry_ref"]
    return full["atom_map"][0], full["species"]


def _assert_atom_map_stored(live, outcome, response, *, doc, reaction, ts_entry_id, reaction_entry_id):
    """Read back the map the adapter sent from ``ts_atom_map`` and compare it with what TCKDB stored.

    Reaching ``uploaded`` already means the server's own atom-map rules passed (a failure is a 422, which
    ``_uploaded`` reports as a setup failure). They are, in the standalone route's
    ``persist_transition_state_upload`` and the bundle's ``persist_computed_reaction_upload`` (both through
    ``app/services/reaction_atom_map.py``): ``atom_map_participant_not_declared`` (a map names a slot the
    reaction does not declare), ``atom_map_indices_not_geometry_relative`` (an atom or TS atom the geometry
    does not have, or a geometry key the deposit does not define), ``atom_map_element_not_conserved``
    (a participant atom mapped onto a TS atom of another element), ``atom_map_contradicts_irc_mapping`` (the
    map against the IRC evidence's participant mapping) and ``atom_map_without_transition_state``; and, at
    the schema boundary, ``atom_map_not_a_bijection``, ``atom_map_atoms_unaccounted_for`` and
    ``atom_map_inferred_requires_note``. A map the adapter did not send is only a warning, which is
    asserted absent below.
    """
    payload_file = outcome.sidecar_path.with_name(outcome.sidecar_path.name.replace(".meta.json", ".payload.json"))
    sent = json.loads(payload_file.read_text())["atom_map"]
    stored, species = _stored_atom_map(live, reaction_entry_id)
    ts_map = reaction["ts_atom_map"]

    # Nothing about the map was withheld, contradicted or reported incomplete, by the adapter or the server.
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert not _ATOM_MAP_WARNINGS & _codes(outcome.warnings), outcome.warnings
    assert not _ATOM_MAP_WARNINGS & _codes(sidecar["warnings"]), sidecar["warnings"]
    assert sidecar["response_status_code"] in (200, 201)

    # Stored as an algorithm's claim, on this transition state, with the adapter's note.
    assert stored["source"] == sent["source"] == "inferred"
    assert stored["note"] == sent["note"] and "symmetry-equivalent" in stored["note"]
    assert stored["equivalent_map_count"] is None
    entry = live.get(f"/scientific/transition-state-entries/{ts_entry_id}")["record"]
    assert stored["transition_state_entry_ref"] == entry["transition_state_entry"]["transition_state_entry_ref"]

    # Every participant's atom_to_ts is what was sent: one block per participant, repeats included.
    by_slot = {}
    for pair in stored["pairs"]:
        by_slot.setdefault((pair["side"], pair["participant_index"]), []).append(pair)
    assert set(by_slot) == {(p["side"], p["participant_index"]) for p in sent["participants"]}
    for participant in sent["participants"]:
        block = by_slot[(participant["side"], participant["participant_index"])]
        assert {p["atom_index"]: p["ts_atom_index"] for p in block} == {
            int(atom): ts for atom, ts in participant["atom_to_ts"].items()}, participant

    # ...and it is ARC's own map: the TS atoms of the concatenated reactants / products, in the order of the
    # species labels, counted from the species' geometries (an oracle independent of the adapter).
    species_by_label = {s["label"]: s for s in doc["species"]}
    ts_symbols = [line.split()[0] for line in next(
        t for t in doc["transition_states"] if t["label"] == reaction["ts_label"])["xyz"].splitlines() if line.strip()]
    for side, labels in (("reactant", reaction["reactant_species_labels"]),
                         ("product", reaction["product_species_labels"])):
        offset = 0
        for position, label in enumerate(labels, start=1):
            symbols = [line.split()[0] for line in species_by_label[label]["xyz"].splitlines() if line.strip()]
            block = sorted(by_slot[(side, position)], key=lambda p: p["atom_index"])
            assert [p["atom_index"] for p in block] == list(range(1, len(symbols) + 1)), (side, position, label)
            assert [p["ts_atom_index"] for p in block] == [
                i + 1 for i in ts_map[f"{side}s"][offset:offset + len(symbols)]], (side, position, label)
            assert [(p["element"], ts_symbols[p["ts_atom_index"] - 1]) for p in block] == [
                (symbol, symbol) for symbol in symbols], (side, position, label)
            offset += len(symbols)
    total = len(reaction["atom_map"])
    assert stored["reactant_atoms_mapped"] == stored["product_atoms_mapped"] == total
    # Whether the TS atom order follows the reactants is ARC's own statement; the stored indices agree.
    identity = all(p["ts_atom_index"] == i for i, p in enumerate(
        sorted((p for p in stored["pairs"] if p["side"] == "reactant"),
               key=lambda p: (p["participant_index"], p["atom_index"])), start=1))
    assert identity is ts_map["ts_atom_order_follows_reactants"]

    # The participants the map points at are the reaction's: a repeated reactant is two participants of one
    # species entry, each with its own block and its own TS atoms.
    for side, labels in (("reactant", reaction["reactant_species_labels"]),
                         ("product", reaction["product_species_labels"])):
        members = species[f"{side}s"]
        assert [m["participant_index"] for m in members] == list(range(1, len(labels) + 1))
        assert [m["smiles"] for m in members] == [species_by_label[label]["smiles"] for label in labels]
        for label, count in Counter(labels).items():
            slots = [i for i, name in enumerate(labels, start=1) if name == label]
            assert len({members[i - 1]["species_entry_ref"] for i in slots}) == 1
            assert len(slots) == count
            if count > 1:
                blocks = [{p["ts_atom_index"] for p in by_slot[(side, i)]} for i in slots]
                assert all(len(block) == _atom_count(species_by_label[label]["xyz"]) for block in blocks)
                assert not set.intersection(*blocks)  # two molecules, not one counted twice
                assert {p["geometry_ref"] for i in slots for p in by_slot[(side, i)]} <= {
                    p["geometry_ref"] for p in by_slot[(side, slots[0])]}
    return stored


@pytest.mark.parametrize("name,index", ATOM_MAP_CASES)
def test_reaction_bundle_stores_the_ts_atom_map(live_tckdb, tmp_path, name, index):
    project, doc = materialize(tmp_path, name)
    reaction = doc["reactions"][index]
    adapter = make_adapter(live_tckdb.url, project, name, "computed_reaction")
    outcome = adapter.submit_computed_reaction_from_output(output_doc=doc, reaction_record=reaction)
    response = _uploaded(live_tckdb, outcome)
    assert response["atom_map_id"] is not None
    _assert_atom_map_stored(
        live_tckdb, outcome, response, doc=doc, reaction=reaction,
        ts_entry_id=response["transition_state_entry_id"], reaction_entry_id=response["reaction_entry_id"])


@pytest.mark.parametrize("name,index", ATOM_MAP_CASES)
def test_standalone_ts_stores_the_ts_atom_map(live_tckdb, tmp_path, name, index):
    project, doc = materialize(tmp_path, name)
    reaction = doc["reactions"][index]
    ts_record = next(t for t in doc["transition_states"] if t["label"] == reaction["ts_label"])
    adapter = make_adapter(live_tckdb.url, project, name, "computed_ts")
    outcome = adapter.submit_computed_ts_from_output(
        output_doc=doc, ts_record=ts_record, reaction_record=reaction)
    response = _uploaded(live_tckdb, outcome)
    _assert_atom_map_stored(
        live_tckdb, outcome, response, doc=doc, reaction=reaction,
        ts_entry_id=response["id"], reaction_entry_id=response["reaction_entry_id"])


# ---------------------------------------------------------------------------
# conformer mode + artifacts
# ---------------------------------------------------------------------------


def _conformer_with_artifacts(live, tmp_path, *, uploads=1):
    """Upload golden H2 in conformer mode, then its log and input ``uploads`` times."""
    project, doc = materialize(tmp_path, "golden")
    doc = attach_log(project, doc, "H2")
    record = next(s for s in doc["species"] if s["label"] == "H2")
    adapter = make_adapter(live.url, project, "golden", "conformer", artifacts=True)
    response = _uploaded(live, adapter.submit_from_output(output_doc=doc, species_record=record))
    calc_id = response["primary_calculation"]["calculation_id"]
    batches = []
    for _ in range(uploads):
        adapter = make_adapter(live.url, project, "golden", "conformer", artifacts=True)
        batch = adapter.submit_artifact_batch_for_calculation(
            output_doc=doc, species_record=record, calculation_id=calc_id,
            calculation_type="opt",
            artifacts=[("output_log", record["opt_log"]), ("input", record["opt_input"])],
        )
        _require([a.status for a in batch] == ["uploaded", "uploaded"],
                 f"artifact upload failed: {[(a.status, a.error) for a in batch]}")
        batches.append(batch)
        # Let this batch commit before the next one, so the next is a true replay.
        listed = live.get_until(f"/calculations/{calc_id}/artifacts", lambda body: len(body) >= 2)
        _require(len(listed) >= 2, f"artifact batch never became readable: {listed!r}")
    # A late duplicate would land after the listing first reaches two rows.
    stored = live.settled(f"/calculations/{calc_id}/artifacts")
    return project, record, response, calc_id, batches, stored


def test_conformer_mode_artifacts_are_stored_and_replay(live_tckdb, tmp_path):
    project, record, response, calc_id, _, stored = _conformer_with_artifacts(
        live_tckdb, tmp_path, uploads=2)
    assert live_tckdb.get(f"/calculations/{calc_id}")["species_entry_id"] == response["species_entry_id"]
    local = {}
    for kind, key in (("output_log", "opt_log"), ("input", "opt_input")):
        data = (project / record[key]).read_bytes()
        local[kind] = (hashlib.sha256(data).hexdigest(), len(data))
    assert {a["kind"]: (a["sha256"], a["bytes"]) for a in stored} == local
    assert len(stored) == 2  # the second batch replayed; nothing was added


def test_artifact_sidecars_capture_server_warnings(live_tckdb, tmp_path):
    project, *_ = _conformer_with_artifacts(live_tckdb, tmp_path)
    sidecars = [sc for name, sc in read_sidecars(project).items() if ".artifact." in name]
    body_warnings = [w for sc in sidecars for r in sc["response_body"]
                     for w in r["response"].get("warnings", [])]
    _require(sidecars and body_warnings,
             "the sample log should draw a multiplicity_mismatch warning in the response")
    assert all(sc.get("warnings") for sc in sidecars), [sc.get("warnings") for sc in sidecars]


# tckdb-client's (0.93-0.95) upload_artifacts keeps only the response body, so the
# adapter (0.6.7) sends each calculation's batch through request_json instead and
# reads the status, request id and replay header off the full response.
_TRANSPORT_FIELDS = {  # field -> (sidecar value, is it right)
    "status_code": (lambda sc: sc.get("response_status_code"), lambda v: v in (200, 201)),
    "request_id": (lambda sc: [r.get("operation") for r in sc.get("request_ids") or []],
                   lambda v: "artifact_upload" in v),
    "replay_flag": (lambda sc: sc.get("idempotency_replayed"), lambda v: v is True),
}


@pytest.mark.parametrize("field", sorted(_TRANSPORT_FIELDS))
def test_artifact_sidecars_capture_transport_metadata(live_tckdb, tmp_path, field):
    # Two uploads, so the second is a replay and the replay flag must be True.
    project, *_ = _conformer_with_artifacts(live_tckdb, tmp_path, uploads=2)
    sidecars = [sc for name, sc in read_sidecars(project).items() if ".artifact." in name]
    _require(len(sidecars) == 2, f"expected 2 artifact sidecars, found {len(sidecars)}")
    value, is_right = _TRANSPORT_FIELDS[field]
    values = [value(sc) for sc in sidecars]
    assert all(is_right(v) for v in values), f"{field}: {values}"


# ---------------------------------------------------------------------------
# Refusals and known gaps
# ---------------------------------------------------------------------------


def test_refusal_is_recorded_in_sidecar(live_tckdb, tmp_path):
    outcome = _submit_species(live_tckdb, tmp_path, "synthetic_species_as_shipped")["ethanol"]
    assert outcome.status == "failed"
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["status"] == "failed"
    assert sidecar["response_status_code"] == 422
    assert "species_geometry_composition_mismatch" in sidecar["last_error"]


def test_computed_species_thermo_names_arkane(live_tckdb, tmp_path):
    # Adapter 0.6.1 names Arkane (arkane_version / arkane_git_commit) on the
    # computed-species thermo and statmech blocks, as the reaction bundle's
    # analysis_software_release already did.
    outcome = _submit_species(live_tckdb, tmp_path, "arc_1_2_corrections")["CH4"]
    thermo = live_tckdb.get(f"/thermo/{_uploaded(live_tckdb, outcome)['thermo']['thermo_id']}")
    assert thermo["software_release_id"] is not None
    assert "missing_software_release_provenance" not in _codes(outcome.warnings)


@pytest.mark.parametrize("mode", ["computed_species", "conformer"])
def test_declared_energy_level_agrees_with_the_sp_it_links(live_tckdb, tmp_path, mode):
    # Adapter 0.6.6 declares thermo/statmech energy_level_of_theory. TCKDB
    # hashes spin_treatment into a level's identity, and the adapter stamps it
    # on the sp from scf_reference, so a declaration built from the bare level
    # would be refused (thermo_/statmech_energy_level_contradiction, 422) and
    # the upload would not reach "uploaded". Not run offline: it needs the
    # live backend.
    project, doc = materialize(tmp_path, "arc_1_2_corrections")
    record = next(s for s in doc["species"] if s["label"] == "CH4")
    record["scf_reference"] = {"freq_reference": "restricted", "sp_reference": "restricted"}
    record["statmech"] = {"external_symmetry": 12, "optical_isomers": 1, "is_linear": False,
                          "rigid_rotor_kind": "asymmetric_top", "torsions": []}
    adapter = make_adapter(live_tckdb.url, project, "arc_1_2_corrections", mode)
    submit = (adapter.submit_computed_species_from_output if mode == "computed_species"
              else adapter.submit_from_output)
    outcome = submit(output_doc=doc, species_record=record)
    _uploaded(live_tckdb, outcome)
    assert not [c for c in _codes(outcome.warnings) if c and "energy_level" in c]


# ---------------------------------------------------------------------------
# Replay: the whole sweep, twice, per corpus and mode
# ---------------------------------------------------------------------------

REPLAY_MATRIX = [
    ("golden", "computed_species"), ("golden", "conformer"),
    ("golden", "computed_reaction"), ("golden", "computed_ts"),
    ("arc_1_2", "computed_species"), ("arc_1_2", "conformer"),
    ("golden_ts_evidence", "computed_reaction"), ("golden_ts_evidence", "computed_ts"),
    ("synthetic_reaction", "computed_reaction"), ("synthetic_reaction", "computed_ts"),
    ("ts_atom_map_sample", "computed_reaction"), ("ts_atom_map_sample", "computed_ts"),
    ("ts_atom_map_derived", "computed_reaction"), ("ts_atom_map_derived", "computed_ts"),
]


def _sweep(live, project, name, mode):
    cfg = make_config(live.url, name, mode)
    adapter = make_adapter(live.url, project, name, mode)
    run_upload_sweep(adapter=adapter, project_directory=str(project), tckdb_config=cfg)
    sidecars = read_sidecars(project)
    _require(sidecars, f"{name}/{mode}: the sweep wrote no sidecars")
    for sidecar_name, sidecar in sidecars.items():
        _require(sidecar["status"] == "uploaded",
                 f"{sidecar_name}: {sidecar['status']} {sidecar.get('last_error')}")
        live.await_commit(sidecar["response_body"])
    return sidecars


@pytest.mark.parametrize("name,mode", REPLAY_MATRIX)
def test_sweep_replay_is_idempotent(live_tckdb, tmp_path, name, mode):
    project, _ = materialize(tmp_path, name)
    first = _sweep(live_tckdb, project, name, mode)
    before = live_tckdb.settled_counts()

    second = _sweep(live_tckdb, project, name, mode)
    assert set(second) == set(first)
    for sidecar_name, sidecar in second.items():
        assert sidecar["idempotency_replayed"] is True, sidecar_name
        assert sidecar["idempotency_key"] == first[sidecar_name]["idempotency_key"]
        assert sidecar["response_body"] == first[sidecar_name]["response_body"]
        server = sidecar["response_body"].get("warnings", [])
        assert all(w in sidecar["warnings"] for w in server), sidecar_name
    assert live_tckdb.settled_counts() == before
