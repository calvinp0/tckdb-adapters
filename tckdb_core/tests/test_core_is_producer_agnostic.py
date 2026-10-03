"""Guard: ``tckdb_core`` must stay free of any one producer.

A second producer (RMG, ChemTrayzer) reuses this package verbatim, so nothing
here may import ARC or ``tckdb_arc``, and nothing may name ARC's output format
or its evidence files. The README may; it explains the rule.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

# ``tckdb_core.__file__`` is ``None`` when pytest runs from the repository root, where the
# ``tckdb_core`` directory resolves as a namespace package; the package directory is found
# from this file's own location instead (and the check below fails if it is wrong).
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "tckdb_core"
SOURCES = sorted(PACKAGE_DIR.rglob("*.py"))

# ``arc.`` is matched on a word boundary so ``tckdb_arc.`` and words ending in
# "arc" (search., march.) do not count.
FORBIDDEN_STRINGS = [
    ("output.yml", re.compile(re.escape("output.yml"))),
    ("parser_evidence", re.compile(re.escape("parser_evidence"))),
    ("restart.yml", re.compile(re.escape("restart.yml"))),
    ("arc.", re.compile(r"\barc\.")),
]


# ``tckdb_arc`` may appear nowhere in core source (code, comments or docstrings; say "the ARC
# adapter"). ``\barc\.`` cannot catch it because ``_`` is a word character. Currently empty.
TCKDB_ARC_ALLOWLIST: set[str] = set()


def _names_arc(module: str) -> bool:
    root = module.split(".")[0]
    return root in {"arc", "tckdb_arc"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_package_has_sources_to_check():
    assert {p.name for p in SOURCES} >= {"uploader.py", "payload_writer.py", "adapter_warnings.py"}


def test_the_checked_directory_is_the_one_the_package_imports_from():
    """The scans above read ``PACKAGE_DIR``; it must be the package under test, wherever pytest starts."""
    import tckdb_core.uploader

    assert Path(tckdb_core.uploader.__file__).resolve().parent == PACKAGE_DIR.resolve()


def test_no_module_imports_arc_or_tckdb_arc():
    offenders = {}
    for path in SOURCES:
        bad = sorted(
            m for m in _imported_modules(path)
            if m == "arc" or m.startswith("arc.") or m == "tckdb_arc" or m.startswith("tckdb_arc.")
        )
        if bad:
            offenders[path.name] = bad
    assert not offenders, offenders


def test_no_source_names_a_producer_output_format():
    offenders = []
    for path in SOURCES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for label, pattern in FORBIDDEN_STRINGS:
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}: {label!r}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_no_source_mentions_tckdb_arc():
    offenders = []
    for path in SOURCES:
        if path.name in TCKDB_ARC_ALLOWLIST:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "tckdb_arc" in line:
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def _dynamic_import_literals(path: Path) -> list[str]:
    """String literals handed to ``import_module`` / ``__import__`` / ``find_spec``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"import_module", "__import__", "find_spec"}:
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append(arg.value)
    return found


def test_no_dynamic_import_names_arc_or_tckdb_arc():
    offenders = {
        path.name: bad for path in SOURCES
        if (bad := [m for m in _dynamic_import_literals(path) if _names_arc(m)])
    }
    assert not offenders, offenders


def test_the_dynamic_import_scan_catches_what_it_claims(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        'import importlib\n'
        'importlib.import_module("tckdb_arc.adapter")\n'
        '__import__("arc")\n'
        'importlib.import_module("os")\n'
    )
    assert [m for m in _dynamic_import_literals(sample) if _names_arc(m)] == ["tckdb_arc.adapter", "arc"]


def test_importing_every_core_module_loads_no_arc_module():
    """Runtime check in a fresh interpreter: the static scans cannot see computed imports."""
    code = (
        "import importlib, pkgutil, sys, tckdb_core\n"
        "for m in pkgutil.walk_packages(tckdb_core.__path__, 'tckdb_core.'):\n"
        "    importlib.import_module(m.name)\n"
        "bad = sorted(n for n in sys.modules if n.split('.')[0] in ('arc', 'tckdb_arc'))\n"
        "print(repr(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
