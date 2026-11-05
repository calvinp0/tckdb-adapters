"""The adapter declares thermo's enthalpy basis itself; no client default may change that.

TCKDB's depositor-side tools may come to default ``enthalpy_reference_kind`` (the coding brief
``enthalpy_reference_client_default_2026-10-25``: ``tckdb_client.builders.thermo`` fills
``formation_298k`` when a builder is given enthalpy content and no kind). The adapter does not use those
builders: it maps ARC's thermo itself and states the basis only when its guards (non-finite, magnitude,
header-level, dispersion/solvation, light-species checks, or output 1.2's ``atom_corrections_applied``)
let it, stripping the enthalpy otherwise (``CLAUDE.md``, "Pre-1.2 enthalpies"). A client default would
declare ``formation_298k`` on exactly the blocks the adapter refuses to. These tests keep the adapter
from reaching a builder, and keep its own declaration on every block that carries enthalpy.
"""

import ast
from pathlib import Path

import pytest
from tckdb_schemas.enthalpy_reference import enthalpy_reference_error

from test_thermo_enthalpy_declaration import _corpus_payloads, _thermo_blocks

PACKAGE_ROOTS = [Path(__file__).resolve().parents[1] / "tckdb_arc",
                 Path(__file__).resolve().parents[2] / "tckdb_core" / "tckdb_core"]


def _imports_of_the_client_builders(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("tckdb_client.builders") or (
                    module == "tckdb_client" and any(a.name == "builders" for a in node.names)):
                found.append(f"{path}:{node.lineno} from {module} import ...")
        elif isinstance(node, ast.Import):
            found += [f"{path}:{node.lineno} import {a.name}" for a in node.names
                      if a.name.startswith("tckdb_client.builders")]
    return found


def test_no_adapter_module_imports_the_clients_builders():
    modules = [p for root in PACKAGE_ROOTS for p in root.rglob("*.py")]
    assert modules, "the package roots moved"
    assert [hit for p in modules for hit in _imports_of_the_client_builders(p)] == []


def _poison_every_builder(monkeypatch):
    """Make every way of constructing a client ``Thermo`` raise; returns what was poisoned."""
    from tckdb_client.builders.thermo import Thermo

    def boom(*_a, **_k):
        raise AssertionError("the adapter reached a tckdb_client.builders Thermo builder")

    poisoned = ["__init__"]
    monkeypatch.setattr(Thermo, "__init__", boom)
    for name, member in list(vars(Thermo).items()):
        if not name.startswith("_") and isinstance(member, (classmethod, staticmethod)):
            monkeypatch.setattr(Thermo, name, classmethod(boom))
            poisoned.append(name)
    return poisoned


def test_thermo_is_built_without_any_client_thermo_builder_and_declares_its_own_basis(tmp_path, monkeypatch):
    poisoned = _poison_every_builder(monkeypatch)
    assert len(poisoned) > 1, "the client no longer has Thermo constructors to poison; revisit this test"
    payloads = _corpus_payloads(tmp_path)
    blocks = [block for _model, payload in payloads for block in _thermo_blocks(payload)]
    assert blocks
    with_enthalpy = [b for b in blocks if "h298_kj_mol" in b or "nasa" in b
                     or any(p.get("h_kj_mol") is not None or p.get("g_kj_mol") is not None
                            for p in b.get("points") or ())]
    assert with_enthalpy
    for block in with_enthalpy:
        assert block["enthalpy_reference_kind"] == "formation_298k", block
        assert enthalpy_reference_error(block) is None
    # The blocks the adapter's guards strip carry S and Cp only, and no declaration: a client default
    # keyed on "has enthalpy content" would find nothing to declare, and none is invented here either.
    for block in blocks:
        if block not in with_enthalpy:
            assert "enthalpy_reference_kind" not in block


@pytest.mark.parametrize("name", ["enthalpy_reference_kind", "reference_pressure_bar"])
def test_the_declaration_fields_are_set_by_the_adapters_own_mapping(name):
    source = (PACKAGE_ROOTS[0] / "adapter.py").read_text(encoding="utf-8")
    assert f'"{name}"' in source
