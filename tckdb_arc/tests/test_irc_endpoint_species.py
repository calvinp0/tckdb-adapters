"""IRC endpoint species are skipped when restart.yml marks them (A17).

ARC creates ``IRC_<ts>_<n>`` with ``irc_label=<ts>`` and appends the endpoint to
the TS's own ``irc_label`` (ARC scheduler.py:4167-4178); ``irc_label`` is written
to ``restart.yml`` by ``ARCSpecies.as_dict`` but not to ``output.yml``.
"""

import copy
import os
from unittest import mock

import pytest
import yaml

from test_adapter import _fake_output_doc, _full_record
from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig

CODE = "irc_endpoint_species_skipped"


def _restart(species):
    return {"species": species}


def _write(project, species):
    if species is not None:
        (project / "restart.yml").write_text(yaml.safe_dump(_restart(species)))


def _doc():
    doc = _fake_output_doc()
    doc["transition_states"] = [{"label": "TS0", "converged": True}]
    return doc


def _endpoint():
    record = copy.deepcopy(_full_record())
    record["label"] = "IRC_TS0_1"
    return record


def _adapter(project, mode):
    return TCKDBAdapter(TCKDBConfig(
        enabled=True, upload=False, base_url="http://x", payload_dir=str(project / "p"),
        upload_mode=mode, api_key_env="X_TCKDB_API_KEY"), project_directory=project)


def _submit(project, mode, record):
    adapter = _adapter(project, mode)
    fn = (adapter.submit_computed_species_from_output if mode == "computed_species"
          else adapter.submit_from_output)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "k"}):
        return fn(output_doc=_doc(), species_record=record)


MARKED = [
    {"label": "IRC_TS0_1", "is_ts": False, "irc_label": "TS0"},
    {"label": "TS0", "is_ts": True, "irc_label": "IRC_TS0_1 IRC_TS0_2"},
]


@pytest.mark.parametrize("mode", ["computed_species", "conformer"])
def test_endpoint_marked_in_restart_is_skipped_with_warning(tmp_path, mode):
    _write(tmp_path, MARKED)
    outcome = _submit(tmp_path, mode, _endpoint())
    assert outcome.status == "skipped" and outcome.payload_path is None
    (warning,) = outcome.warnings
    assert warning["code"] == CODE and warning["context"]["ts_label"] == "TS0"
    assert not list(tmp_path.rglob("*.payload.json"))


def test_without_restart_yml_it_is_uploaded_like_any_species(tmp_path):
    outcome = _submit(tmp_path, "computed_species", _endpoint())
    assert outcome.payload_path is not None and outcome.payload_path.exists()
    assert CODE not in [w["code"] for w in outcome.warnings]


def test_restart_without_the_species_entry_is_not_an_answer(tmp_path):
    _write(tmp_path, [MARKED[1]])
    assert _submit(tmp_path, "computed_species", _endpoint()).payload_path is not None


@pytest.mark.parametrize("species", [
    [dict(MARKED[0], is_ts=True), MARKED[1]],                      # a TS itself
    [dict(MARKED[0], irc_label=None), MARKED[1]],                  # no irc_label
    [dict(MARKED[0], irc_label="TS9"), MARKED[1]],                 # not a TS of this run
    [MARKED[0], dict(MARKED[1], irc_label="IRC_TS0_2")],           # TS does not list it
    [dict(MARKED[0], is_ts=None), MARKED[1]],                      # is_ts not stated
])
def test_only_a_consistent_restart_entry_counts(tmp_path, species):
    _write(tmp_path, species)
    assert _submit(tmp_path, "computed_species", _endpoint()).payload_path is not None


def test_an_ordinary_species_named_like_an_endpoint_is_not_skipped_without_the_marker(tmp_path):
    _write(tmp_path, [{"label": "IRC_TS0_1", "is_ts": False, "irc_label": None}])
    assert _submit(tmp_path, "computed_species", _endpoint()).payload_path is not None


def test_ts_with_restart_entry_is_never_treated_as_endpoint(tmp_path):
    _write(tmp_path, MARKED)
    record = copy.deepcopy(_full_record())
    record["label"] = "TS0"
    assert _submit(tmp_path, "computed_species", record).payload_path is not None
