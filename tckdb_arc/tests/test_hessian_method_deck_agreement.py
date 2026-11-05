"""``freq.hessian_method`` and the shipped freq input deck agree (TCKDB 0.144).

ARC owns the Hessian method (output.yml ``freq_hessian_method``). The adapter passes it through
as the typed ``freq.hessian_method`` parameter and never reads the deck to derive it. TCKDB reads the
same deck (``tckdb_schemas.hessian_method_detection``) and refuses an explicit value that disagrees
with a directive in it (422 ``hessian_method_contradicts_input_deck``). A deck with no directive
(a bare Gaussian ``freq``, ORCA ``Freq``) contradicts nothing.

These tests build payloads from records whose ``freq_input`` deck is on disk and shipped as the
``input`` artifact, and assert:

* where ARC's value matches the deck's directive, the parameter sent equals what the shipped deck
  reader reads from that deck, and the pinned models accept the payload (the conftest hook);
* where there is no directive, any token or none is accepted;
* ARC's silence is not filled in from the deck (the adapter never re-derives the method);
* where ARC's value contradicts the deck, the adapter forwards it unchanged and the shipped
  models refuse it locally with the server's code. That is the present behaviour, documented
  here, not endorsed: the value is ARC's to get right (ARC PR #1059), and nothing in the adapter
  drops or rewrites it.
"""

import copy
import json
import os
from unittest import mock

import pytest

from _contract import contract_validate
from test_arc_schema_1_3_thermo import Built, _doc, _species
from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBArtifactConfig, TCKDBConfig
from tckdb_client.rejection_codes import RejectionCode
from tckdb_schemas.hessian_method_detection import detect_hessian_method
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.computed_species_upload import ComputedSpeciesUploadRequest

ANALYTIC = "analytic"
FD_GRADIENT = "finite_difference_gradient"
FD_ENERGY = "finite_difference_energy"

_GAUSSIAN_BODY = "\n0 1\nC 0.0 0.0 0.0\nH 0.0 0.0 1.09\n\n"
_ORCA_BODY = "\n* xyz 0 1\nC 0.0 0.0 0.0\nH 0.0 0.0 1.09\n*\n"

GAUSSIAN_NUMER = "%mem=2GB\n#p wb97xd/def2tzvp Freq=Numer\n\nfreq" + _GAUSSIAN_BODY
GAUSSIAN_BARE = "%mem=2GB\n#p wb97xd/def2tzvp freq\n\nfreq" + _GAUSSIAN_BODY
ORCA_NUMFREQ = "! wB97X-D3 def2-TZVP NumFreq\n" + _ORCA_BODY
ORCA_NUMFREQ_NUMGRAD = "! wB97X-D3 def2-TZVP NumFreq NumGrad\n" + _ORCA_BODY
ORCA_BARE = "! wB97X-D3 def2-TZVP Freq\n" + _ORCA_BODY
# Near-misses of a directive: tokens that contain or resemble one but are not it.
GAUSSIAN_CALCNUMERFC = "%mem=2GB\n#p wb97xd/def2tzvp opt=CalcNumerFC freq\n\nfreq" + _GAUSSIAN_BODY
GAUSSIAN_SCF_NUMER = "%mem=2GB\n#p wb97xd/def2tzvp SCF=(Numer) freq\n\nfreq" + _GAUSSIAN_BODY
GAUSSIAN_FREQCHK = "%mem=2GB\n%chk=freq.chk\n#p wb97xd/def2tzvp FreqChk\n\nfreq" + _GAUSSIAN_BODY
ORCA_COMMENT = "! B3LYP def2-SVP # NumFreq # Freq\n" + _ORCA_BODY

ROUTES = ("computed_species", "computed_reaction")
_REQUEST_MODEL = {
    "computed_species": ComputedSpeciesUploadRequest,
    "computed_reaction": ComputedReactionUploadRequest,
}


def _build(tmp_path, route, *, program, deck, arc_value):
    """Build one route's payload for sBuOH with ``deck`` shipped as its freq ``input`` artifact."""
    doc = _doc()
    record = _species(doc, "sBuOH")
    name = "freq.gjf" if program == "gaussian" else "freq.in"
    (tmp_path / name).write_text(deck)
    record["freq_input"] = name
    record["freq_hessian_method"] = arc_value
    record["ess_software"]["freq"] = program
    cfg = TCKDBConfig(
        enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=str(tmp_path / "payloads"),
        api_key_env="X_TCKDB_API_KEY", project_label="hessian", upload_mode="all", upload=False,
        preflight=False, artifacts=TCKDBArtifactConfig(upload=True, kinds=("input",)),
    )
    adapter = TCKDBAdapter(cfg, project_directory=str(tmp_path))
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        if route == "computed_species":
            outcome = adapter.submit_computed_species_from_output(
                output_doc=doc, species_record=copy.deepcopy(record))
        else:
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=copy.deepcopy(doc["reactions"][0]))
    payload = json.loads(outcome.payload_path.read_text())
    return Built(payload, outcome.warnings, route, "sBuOH")


def _freq(built):
    (freq,) = built.calcs("freq")
    return freq


def _sent(freq):
    values = [p["canonical_value"] for p in freq.get("parameters", ())
              if p["canonical_key"] == "freq.hessian_method"]
    assert len(values) <= 1
    return values[0] if values else None


def _shipped_deck(freq):
    (deck,) = [a for a in freq["artifacts"] if a["kind"] == "input"]
    return deck


# (program, deck, the value the deck's directive states)
DIRECTIVES = [
    pytest.param("orca", ORCA_NUMFREQ, FD_GRADIENT, id="orca-NumFreq"),
    pytest.param("orca", ORCA_NUMFREQ_NUMGRAD, FD_ENERGY, id="orca-NumFreq+NumGrad"),
    pytest.param("gaussian", GAUSSIAN_NUMER, FD_GRADIENT, id="gaussian-Freq=Numer"),
]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("program, deck, stated", DIRECTIVES)
def test_the_sent_hessian_method_equals_what_the_shipped_deck_states(tmp_path, route, program, deck, stated):
    # The shipped reader (the one TCKDB parses decks with) reads the directive we expect.
    assert detect_hessian_method(program, deck).value == stated
    built = _build(tmp_path, route, program=program, deck=deck, arc_value=stated)
    freq = _freq(built)
    assert _shipped_deck(freq)["filename"] == ("freq.gjf" if program == "gaussian" else "freq.in")
    assert _sent(freq) == detect_hessian_method(program, deck).value == stated
    # The pinned models (conftest hook, which also ran) accept the payload as built.
    contract_validate(_REQUEST_MODEL[route], built.payload)


NO_DIRECTIVE = [
    pytest.param("gaussian", GAUSSIAN_BARE, id="gaussian-bare-freq"),
    pytest.param("orca", ORCA_BARE, id="orca-Freq"),
    pytest.param("gaussian", GAUSSIAN_CALCNUMERFC, id="gaussian-opt=CalcNumerFC-freq"),
    pytest.param("gaussian", GAUSSIAN_SCF_NUMER, id="gaussian-SCF=(Numer)-freq"),
    pytest.param("gaussian", GAUSSIAN_FREQCHK, id="gaussian-FreqChk"),
    pytest.param("orca", ORCA_COMMENT, id="orca-NumFreq-in-comment"),
]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("program, deck", NO_DIRECTIVE)
@pytest.mark.parametrize("arc_value", [None, ANALYTIC, FD_GRADIENT, FD_ENERGY])
def test_a_deck_without_a_directive_accepts_any_token_or_none(tmp_path, route, program, deck, arc_value):
    assert detect_hessian_method(program, deck) is None
    built = _build(tmp_path, route, program=program, deck=deck, arc_value=arc_value)
    freq = _freq(built)
    assert _shipped_deck(freq)["kind"] == "input"
    assert _sent(freq) == arc_value
    contract_validate(_REQUEST_MODEL[route], built.payload)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("program, deck, stated", DIRECTIVES)
def test_the_adapter_does_not_fill_in_the_method_from_the_deck(tmp_path, route, program, deck, stated):
    """ARC states no method: the parameter is omitted, not derived from the directive in the deck."""
    built = _build(tmp_path, route, program=program, deck=deck, arc_value=None)
    freq = _freq(built)
    assert detect_hessian_method(program, deck).value == stated
    assert _shipped_deck(freq)["kind"] == "input"
    assert _sent(freq) is None
    contract_validate(_REQUEST_MODEL[route], built.payload)


CONTRADICTIONS = [
    pytest.param("orca", ORCA_NUMFREQ, ANALYTIC, FD_GRADIENT, id="orca-NumFreq-says-analytic"),
    pytest.param("orca", ORCA_NUMFREQ_NUMGRAD, FD_GRADIENT, FD_ENERGY, id="orca-NumGrad-says-gradient"),
    pytest.param("gaussian", GAUSSIAN_NUMER, ANALYTIC, FD_GRADIENT, id="gaussian-Numer-says-analytic"),
    pytest.param("gaussian", GAUSSIAN_NUMER, FD_ENERGY, FD_GRADIENT, id="gaussian-Numer-says-energy"),
]


@pytest.mark.payload_refused_by_contract
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("program, deck, arc_value, stated", CONTRADICTIONS)
def test_a_value_contradicting_the_deck_is_forwarded_and_the_shipped_models_refuse_it(
    tmp_path, route, program, deck, arc_value, stated,
):
    """Present behaviour, reported and not fixed: the adapter forwards ARC's value unchanged.

    The server would answer 422 ``hessian_method_contradicts_input_deck``; the same shared rule runs
    in the published models, so the refusal reproduces offline. The marker makes the conftest hook
    require that refusal. Correcting the value is ARC's job (ARC PR #1059), not the adapter's.
    """
    built = _build(tmp_path, route, program=program, deck=deck, arc_value=arc_value)
    freq = _freq(built)
    assert detect_hessian_method(program, deck).value == stated != arc_value
    assert _sent(freq) == arc_value
    with pytest.raises(ValueError) as refused:
        _REQUEST_MODEL[route].model_validate(built.payload)
    assert RejectionCode.HESSIAN_METHOD_CONTRADICTS_INPUT_DECK.value in str(refused.value)
    assert f"was {arc_value} but" in str(refused.value)
    assert f"deck attached to the same calculation says {stated}" in str(refused.value)

