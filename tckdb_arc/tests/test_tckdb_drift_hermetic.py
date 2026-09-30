"""The drift tool's tests must not depend on the checkout's current pins.

tckdb-drift.yml bumps the real pin files to the newest TCKDB and then runs the
suite, so a test that reads those files as its fixture fails exactly when there is
drift (that happened: issue #17). This bumps a copy of the real files to a far
newer line and runs the drift tests there.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COPIED = [
    "tools/tckdb_drift.py",
    "tckdb_arc/pyproject.toml",
    "tckdb_arc/tests/_contract.py",
    "tckdb_arc/README.md",
    "tckdb_arc/tests/test_tckdb_drift.py",
    "tckdb_arc/tests/_drift_fixture.py",
    "tckdb-pin.toml",
    "README.md",
    "CLAUDE.md",
    ".github/workflows/ci.yml",
    ".github/workflows/tckdb-drift.yml",
]


def test_drift_tests_pass_with_the_real_pins_already_bumped(tmp_path):
    for rel in COPIED:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, dest)

    spec = importlib.util.spec_from_file_location("drift_copy", tmp_path / "tools" / "tckdb_drift.py")
    drift = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drift)
    # Far past any real release, so "already at the available line" holds for every line.
    drift.run_bump(tmp_path, "b" * 40, "9.99.0", "9.99.0")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--noconftest", "-p", "no:cacheprovider",
         str(tmp_path / "tckdb_arc" / "tests" / "test_tckdb_drift.py")],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
