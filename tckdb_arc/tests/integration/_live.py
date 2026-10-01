"""Shared plumbing for the opt-in live TCKDB integration gate.

Everything here is offline: the loopback guard, the corpus builders and the
adapter factory. Network access happens only through the ``live_tckdb``
fixture in ``conftest.py``, which calls :func:`assert_loopback_url` first.

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
from urllib.parse import urlsplit

import yaml

from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBArtifactConfig, TCKDBConfig

API_KEY_ENV = "TCKDB_INTEGRATION_API_KEY"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
REFUSED_PORTS = frozenset({8010})
INTEGRATION_BUCKET_PREFIX = "tckdb-integ"

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
GOLDEN = FIXTURES / "golden"
CURRENT_ARC = FIXTURES / "current_arc"
ARC_1_2 = FIXTURES / "arc_1_2" / "output.yml"
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


def assert_loopback_url(url):
    """Refuse any TCKDB base URL that is not loopback, or that uses port 8010.

    Loopback alone does not mean isolated: TCKDB's API listens on
    127.0.0.1:8010 on the production host and on a development machine, and
    an ``ssh -L 8010:...`` tunnel makes a remote deployment loopback too. The
    port is refused outright; :func:`assert_integration_backend` then checks
    what actually answered.
    """
    parts = urlsplit(url or "")
    host = parts.hostname
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing TCKDB integration target {url!r}: host {host!r} is not "
            f"loopback ({', '.join(sorted(LOOPBACK_HOSTS))}). The gate only "
            "runs against an isolated local backend."
        )
    if parts.port in REFUSED_PORTS:
        raise ValueError(
            f"refusing TCKDB integration target {url!r}: port {parts.port} is "
            "where TCKDB's production and development APIs listen (directly or "
            "through an SSH tunnel). Run the isolated backend on another port."
        )
    return url


def assert_integration_backend(status):
    """Refuse a backend whose artifact bucket is not an integration sentinel.

    ``status`` is the body of ``GET /status``. The isolated stack in
    ``docs/contract/INTEGRATION_GATE.md`` uses bucket ``tckdb-integ-artifacts``;
    deployments use ``tckdb-artifacts``.
    """
    storage = ((status or {}).get("components") or {}).get("artifact_storage") or {}
    bucket = storage.get("bucket")
    if not (isinstance(bucket, str) and bucket.startswith(INTEGRATION_BUCKET_PREFIX)):
        raise ValueError(
            f"refusing TCKDB integration target: its artifact bucket is {bucket!r}, "
            f"not an isolated integration bucket ({INTEGRATION_BUCKET_PREFIX}*)."
        )
    return bucket


def verify_isolated_backend(url, timeout=30.0):
    """Check that ``url`` is an isolated integration backend, without credentials.

    The loopback/port check runs first, then ``/status`` (bucket sentinel) and
    ``/readyz`` are fetched by a client that holds no API key: tckdb-client
    attaches ``X-API-Key`` to every request whenever it has one, even
    unauthenticated ones, so a keyed probe would hand the key to whatever
    answered before it was verified.
    """
    from tckdb_client import TCKDBClient
    from tckdb_client.errors import TCKDBHTTPError

    assert_loopback_url(url)
    with TCKDBClient(url, api_key=None, timeout=timeout) as probe:
        try:
            status = probe.request_json("GET", "/status", authenticated=False).data
        except TCKDBHTTPError as exc:  # a degraded backend answers 503 with the same body
            status = exc.response_json
        assert_integration_backend(status)
        ready = probe.request_json("GET", "/readyz", authenticated=False).data
    if not (isinstance(ready, dict) and ready.get("status") == "ready"):
        raise RuntimeError(f"TCKDB at {url} is not ready: {ready!r}")
    return url


def _load_yaml(path):
    return yaml.safe_load(Path(path).read_text())


def golden():
    return _load_yaml(GOLDEN / "phase3_output.yml"), [GOLDEN / "tckdb_evidence.json"]


def golden_kinetics():
    doc, sidecars = golden()
    doc["reactions"][0]["kinetics"] = dict(KINETICS_T0)
    doc["arkane_git_commit"] = ARKANE_GIT_COMMIT
    return doc, sidecars


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


def golden_irc_passed():
    return _golden_with_irc_verdict(True)


def golden_irc_failed():
    return _golden_with_irc_verdict(False)


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


def synthetic_species_as_shipped():
    from test_adapter import _fake_output_doc, _full_record

    doc = _fake_output_doc()
    doc["species"] = [_full_record()]
    return doc, []


CORPORA = {
    "golden": golden,
    "golden_kinetics": golden_kinetics,
    "golden_irc_passed": golden_irc_passed,
    "golden_irc_failed": golden_irc_failed,
    "arc_1_2": arc_1_2,
    "arc_1_2_corrections": arc_1_2_corrections,
    "current_arc_h2o": current_arc_h2o,
    "synthetic_reaction": synthetic_reaction,
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


def commit_probe(response):
    """An id-addressed path that exists once ``response``'s upload has committed.

    TCKDB commits in the teardown of its write-session dependency, after the
    201 has been sent (see T4 in INTEGRATION_GATE.md), so a read issued the
    moment an upload returns can miss the rows. The transaction is atomic:
    once one of its rows is visible, all are.
    """
    if not isinstance(response, dict):
        return None
    if response.get("conformers"):
        return f"/calculations/{response['conformers'][0]['primary_calculation']['calculation_id']}"
    if response.get("primary_calculation"):
        return f"/calculations/{response['primary_calculation']['calculation_id']}"
    if response.get("calculation_keys"):
        return f"/calculations/{min(response['calculation_keys'].values())}"
    if response.get("type") == "transition_state_entry":
        return f"/transition-states/entries/{response['id']}"
    return None


def read_sidecars(project):
    """Current (non-archived) upload sidecars under a project's payload dir."""
    return {
        path.name: json.loads(path.read_text())
        for path in sorted((Path(project) / "tckdb_payloads").rglob("*.meta.json"))
        if "archive" not in path.parts
    }
