"""Unit tests for tools/tckdb_drift.py: pin parsing and rewriting. No network."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

import _drift_fixture as fx

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("tckdb_drift", REPO / "tools" / "tckdb_drift.py")
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)

NEW_SHA = "74aae9a2" + "0" * 32
FILES = fx.BUMPED
UNTOUCHED = [rel for rel in fx.FILES if rel not in fx.BUMPED]


@pytest.fixture
def root(tmp_path):
    """A synthetic repo with fixed pins (0.54 / 0.95); never the checkout's real files."""
    return fx.build(tmp_path)


def _snapshot(root):
    return {rel: (root / rel).read_text() for rel in FILES}


def test_line_and_bound_helpers():
    assert drift.line_of("0.58.3") == (0, 58)
    assert drift.bound_for((0, 58)) == ">=0.58,<0.59"
    with pytest.raises(drift.DriftError):
        drift.parse_version("0.58")


def test_read_pins_from_the_synthetic_repo(root):
    assert drift.read_pins(root) == {
        "schemas_line": fx.PINNED_SCHEMAS, "client_line": fx.PINNED_CLIENT, "sha": fx.OLD_SHA,
    }


@pytest.mark.parametrize("spec", ["tckdb-schemas>=0.54", "tckdb-schemas>=0.54,<0.56", "tckdb-schemas==0.54.0"])
def test_pinned_line_refuses_anything_but_one_minor_line(spec):
    text = f'[project]\ndependencies = ["{spec}"]\n'
    with pytest.raises(drift.DriftError):
        drift.pinned_line(text, "tckdb-schemas")


# The one deliberate read of the real files: a structural check that holds for any pin value.
def test_real_pin_file_parses_and_no_workflow_has_a_literal_sha():
    pin = drift.read_pin_file((REPO / drift.PIN_FILE).read_text())
    assert drift.SHA_RE.match(pin["sha"])
    assert pin["repo"].startswith("https://") and pin["schemas_subdir"] and pin["client_subdir"]
    # ci.yml reads the pin at runtime, so a bump never edits a workflow file.
    for workflow in (REPO / ".github" / "workflows").glob("*.yml"):
        assert not re.search(r"\b[0-9a-f]{40}\b", workflow.read_text()), workflow.name


@pytest.mark.parametrize("bad", ['sha = "abc"', 'repo = ""', "[other]\nx = 1"])
def test_pin_file_rejects_bad_content(bad):
    text = "[tckdb]\n" + bad + "\n" if not bad.startswith("[") else bad
    with pytest.raises(drift.DriftError):
        drift.read_pin_file(text)


def _pins():
    return {"schemas_line": (0, 54), "client_line": (0, 95), "sha": "f" * 40}


def _summary(schemas, client):
    return drift.build_summary(_pins(), "r", "main", "a" * 40, schemas, client)


def test_summary_no_drift_inside_the_pinned_lines():
    s = _summary("0.54.9", "0.95.4")
    assert s["drift"] is False and not s["schemas"]["newer"] and not s["client"]["newer"]
    assert s["sha_changed"] is True  # a new commit alone is not drift


def test_summary_drift_is_per_package():
    s = _summary("0.54.0", "0.98.0")
    assert (s["schemas"]["newer"], s["client"]["newer"], s["drift"]) == (False, True, True)
    s = _summary("0.58.0", "0.95.1")
    assert (s["schemas"]["newer"], s["client"]["newer"], s["drift"]) == (True, False, True)


def test_summary_older_mirror_is_not_drift():
    assert _summary("0.53.0", "0.94.0")["drift"] is False


def test_bump_rewrites_only_the_pins_and_patch(root):
    before = {rel: (root / rel).read_text() for rel in FILES + UNTOUCHED}
    assert drift.run_bump(root, NEW_SHA, "0.58.0", "0.98.0") == 0
    after = {rel: (root / rel).read_text() for rel in FILES + UNTOUCHED}

    changed = {rel for rel in before if before[rel] != after[rel]}
    assert changed == set(FILES)  # no workflow file, no CLAUDE.md

    assert drift.pinned_line(after[drift.ADAPTER_PYPROJECT], "tckdb-schemas") == (0, 58)
    assert drift.pinned_line(after[drift.ADAPTER_PYPROJECT], "tckdb-client") == (0, 98)
    assert drift.pinned_line(after[drift.CORE_PYPROJECT], "tckdb-schemas") == (0, 58)
    assert drift.pinned_line(after[drift.CORE_PYPROJECT], "tckdb-client") == (0, 98)
    assert drift.adapter_version(after[drift.CORE_PYPROJECT]) == drift.adapter_version(before[drift.CORE_PYPROJECT])
    assert "TARGET_SCHEMAS_LINE = (0, 58)" in after[drift.CONTRACT_PY]
    assert drift.read_pin_file(after[drift.PIN_FILE])["sha"] == NEW_SHA
    assert NEW_SHA in after[drift.README]
    assert "`tckdb-client` 0.98.x with `tckdb-schemas` 0.58.x" in after[drift.README]
    assert "`tckdb-client` 0.98.x and `tckdb-schemas` 0.58.x" in after[drift.PKG_README]

    old_v = drift.parse_version(drift.adapter_version(before[drift.ADAPTER_PYPROJECT]))
    new_v = drift.parse_version(drift.adapter_version(after[drift.ADAPTER_PYPROJECT]))
    assert new_v == (old_v[0], old_v[1], old_v[2] + 1)
    entry = f"- Adapter {new_v[0]}.{new_v[1]}.{new_v[2]}: tracked TCKDB 74aae9a (schemas 0.58.0, client 0.98.0)."
    assert f"{drift.CHANGELOG_MARKER}\n{entry}\n" in after[drift.PKG_README]

    # Nothing else moved: every changed line is a pin, the SHA, a version sentence or the changelog line.
    for rel in FILES:
        old_lines, new_lines = before[rel].splitlines(), after[rel].splitlines()
        assert len(new_lines) == len(old_lines) + (1 if rel == drift.PKG_README else 0)
        if rel == drift.PKG_README:
            new_lines = [n for n in new_lines if n != entry]
        for o, n in zip(old_lines, new_lines):
            if o != n:
                assert any(k in o for k in ("tckdb-", "TARGET_SCHEMAS_LINE", "tckdbv2.git@", "version =", "sha =")), o


def test_bump_is_idempotent(root):
    drift.run_bump(root, NEW_SHA, "0.58.0", "0.98.0")
    once = _snapshot(root)
    drift.run_bump(root, NEW_SHA, "0.58.2", "0.98.1")  # same lines, same sha
    assert _snapshot(root) == once


def test_bump_only_the_client_line(root):
    pins = drift.read_pins(root)
    schemas = f"{pins['schemas_line'][0]}.{pins['schemas_line'][1]}.3"
    drift.run_bump(root, NEW_SHA, schemas, "0.98.0")
    after = _snapshot(root)
    assert drift.pinned_line(after[drift.ADAPTER_PYPROJECT], "tckdb-schemas") == pins["schemas_line"]
    assert drift.pinned_line(after[drift.ADAPTER_PYPROJECT], "tckdb-client") == (0, 98)


def test_bump_rejects_a_short_sha_and_writes_nothing(root):
    before = _snapshot(root)
    with pytest.raises(drift.DriftError):
        drift.run_bump(root, "74aae9a2", "0.58.0", "0.98.0")
    assert _snapshot(root) == before


def test_bump_fails_before_writing_when_a_file_lacks_its_anchor(root):
    (root / drift.CONTRACT_PY).write_text("nothing here\n")
    before = _snapshot(root)
    with pytest.raises(drift.DriftError):
        drift.run_bump(root, NEW_SHA, "0.58.0", "0.98.0")
    assert _snapshot(root) == before


def test_resolve_sha_passes_a_full_sha_through_without_the_network():
    assert drift.resolve_sha("https://invalid.invalid/x.git", "c" * 40) == "c" * 40


def test_package_version_reads_project_version():
    assert drift.package_version('[project]\nversion = "0.58.0"\n', "p") == "0.58.0"
    with pytest.raises(drift.DriftError):
        drift.package_version("[tool]\n", "p")


def test_version_comparison_is_numeric_not_textual():
    assert drift.line_of("0.10.0") > drift.line_of("0.9.4")
    pins = {"schemas_line": (0, 9), "client_line": (0, 9), "sha": "f" * 40}
    s = drift.build_summary(pins, "r", "main", "a" * 40, "0.10.0", "0.9.3")
    assert (s["schemas"]["newer"], s["client"]["newer"]) == (True, False)
    assert drift.bound_for((0, 9)) == ">=0.9,<0.10"


def test_package_version_is_normalised_and_ascii_only():
    assert drift.package_version('[project]\nversion = " 0.58.0 "\n', "p") == "0.58.0"
    with pytest.raises(drift.DriftError):
        drift.package_version('[project]\nversion = "0.58.0rc1"\n', "p")
    with pytest.raises(drift.DriftError):
        drift.package_version('[project]\nversion = "0.\u0665\u0668.0"\n', "p")  # Arabic-Indic digits


def test_run_check_reports_drift_and_the_json(root, monkeypatch, capsys):
    pin = drift.read_pin_file((root / drift.PIN_FILE).read_text())
    seen = {}

    def fake_resolve(repo, ref):
        seen["repo"], seen["ref"] = repo, ref
        return NEW_SHA

    def fake_fetch(repo, sha, paths):
        assert sha == NEW_SHA
        assert paths == [f"{pin['schemas_subdir']}/pyproject.toml", f"{pin['client_subdir']}/pyproject.toml"]
        return {paths[0]: '[project]\nversion = "0.58.0"\n', paths[1]: '[project]\nversion = "0.98.0"\n'}

    monkeypatch.setattr(drift, "resolve_sha", fake_resolve)
    monkeypatch.setattr(drift, "fetch_files", fake_fetch)
    assert drift.run_check(root, None, "main") == drift.EXIT_DRIFT
    assert seen["repo"] == pin["repo"]  # the pin file is the source of the repo
    out = json.loads(capsys.readouterr().out)
    assert out["drift"] is True and out["available_sha"] == NEW_SHA
    assert out["schemas"]["available_version"] == "0.58.0" and out["client"]["newer"] is True

    assert drift.run_check(root, "https://example.invalid/x.git", "main") == drift.EXIT_DRIFT
    assert seen["repo"] == "https://example.invalid/x.git"  # explicit override wins

    pins = drift.read_pins(root)
    same = {
        (f"{pin['schemas_subdir']}/pyproject.toml"): "[project]\nversion = \"%d.%d.9\"\n" % pins["schemas_line"],
        (f"{pin['client_subdir']}/pyproject.toml"): "[project]\nversion = \"%d.%d.9\"\n" % pins["client_line"],
    }
    monkeypatch.setattr(drift, "fetch_files", lambda repo, sha, paths: same)
    assert drift.run_check(root, None, "main") == 0


def test_pin_file_rewrite_touches_only_the_tckdb_table_sha():
    other = 'sha = "%s"\n[tckdb]\nrepo = "https://x/y.git"\nsha = "%s"\nschemas_subdir = "s"\nclient_subdir = "c"\n[z]\nsha = "%s"\n'
    old, mid, tail = "1" * 40, "2" * 40, "3" * 40
    text = other % (old, mid, tail)
    new = drift.rewrite_pin_file(text, NEW_SHA)
    assert new == other % (old, NEW_SHA, tail)


def test_bump_refuses_a_downgrade_unless_allowed(root):
    before = {rel: (root / rel).read_text() for rel in FILES}
    pins = drift.read_pins(root)
    lower = f"{pins['schemas_line'][0]}.{pins['schemas_line'][1] - 1}.0"
    with pytest.raises(drift.DriftError, match="--allow-downgrade"):
        drift.run_bump(root, NEW_SHA, lower, "0.98.0")
    assert {rel: (root / rel).read_text() for rel in FILES} == before
    drift.run_bump(root, NEW_SHA, lower, "0.98.0", allow_downgrade=True)
    assert drift.pinned_line((root / drift.ADAPTER_PYPROJECT).read_text(), "tckdb-schemas")[1] == pins["schemas_line"][1] - 1


def test_main_exits_2_on_a_refused_bump(root, capsys):
    rc = drift.main(["--root", str(root), "--bump", "--sha", NEW_SHA, "--schemas", "0.1.0", "--client", "0.98.0"])
    assert rc == 2 and "--allow-downgrade" in capsys.readouterr().err


def test_bump_keeps_extras_and_markers_in_specifiers():
    text = (
        '[project]\ndependencies = [\n'
        '    "tckdb-client[fast]>=0.95,<0.96 ; python_version >= \'3.11\'",\n'
        '    "tckdb-schemas >=0.54,<0.55",\n]\n'
    )
    new = drift.rewrite_pyproject(text, (0, 58), (0, 98))
    assert '"tckdb-client[fast]>=0.98,<0.99 ; python_version >= \'3.11\'"' in new
    assert '"tckdb-schemas>=0.58,<0.59"' in new
    assert drift.pinned_line(new, "tckdb-client") == (0, 98)


def test_rewrite_sha_uses_the_pin_repo_not_a_hardcoded_one():
    repo = "https://example.org/other.git"
    text = f"pip install git+{repo}@{'1' * 40}#x\n pip install git+https://github.com/calvinp0/tckdbv2.git@{'2' * 40}#x\n"
    new = drift.rewrite_sha(text, repo, NEW_SHA, "README")
    assert f"{repo}@{NEW_SHA}" in new and "2" * 40 in new
    with pytest.raises(drift.DriftError):
        drift.rewrite_sha("nothing\n", repo, NEW_SHA, "README")


def test_install_specs_come_from_the_pin_file(root, capsys):
    assert drift.run_install_specs(root, None) == 0
    out = capsys.readouterr().out
    pin = drift.read_pin_file((root / drift.PIN_FILE).read_text())
    assert f"TCKDB_CLIENT_SPEC=tckdb-client @ git+{pin['repo']}@{pin['sha']}#subdirectory={pin['client_subdir']}" in out
    assert f"TCKDB_SCHEMAS_SPEC=tckdb-schemas @ git+{pin['repo']}@{pin['sha']}#subdirectory={pin['schemas_subdir']}" in out
    assert drift.run_install_specs(root, NEW_SHA) == 0
    assert NEW_SHA in capsys.readouterr().out


@pytest.mark.parametrize("package", ["tckdb-schemas", "tckdb-client"])
def test_a_stale_core_pyproject_reports_drift_in_check(root, monkeypatch, package):
    """Core pins the same packages as the adapter, so a stale core pin is drift."""
    path = root / drift.CORE_PYPROJECT
    text = path.read_text()
    # Move core one minor behind the adapter's pin for this package.
    line = drift.pinned_line(text, package)
    older = (line[0], line[1] - 1)
    text = re.sub(rf'"{package}[^"]*"', f'"{package}{drift.bound_for(older)}"', text)
    path.write_text(text)
    assert drift.read_pins(root)["schemas_line" if package == "tckdb-schemas" else "client_line"] == older

    pin = drift.read_pin_file((root / drift.PIN_FILE).read_text())
    available = {
        f"{pin['schemas_subdir']}/pyproject.toml": '[project]\nversion = "%d.%d.9"\n' % fx.PINNED_SCHEMAS,
        f"{pin['client_subdir']}/pyproject.toml": '[project]\nversion = "%d.%d.9"\n' % fx.PINNED_CLIENT,
    }
    monkeypatch.setattr(drift, "resolve_sha", lambda repo, ref: fx.OLD_SHA)
    monkeypatch.setattr(drift, "fetch_files", lambda repo, sha, paths: available)
    assert drift.run_check(root, None, "main") == drift.EXIT_DRIFT


def test_bump_repairs_a_core_pin_that_lags_the_adapter(root):
    path = root / drift.CORE_PYPROJECT
    path.write_text(path.read_text().replace(">=0.54,<0.55", ">=0.53,<0.54"))
    drift.run_bump(root, fx.OLD_SHA, "0.54.0", "0.95.0")
    assert drift.pinned_line(path.read_text(), "tckdb-schemas") == fx.PINNED_SCHEMAS
