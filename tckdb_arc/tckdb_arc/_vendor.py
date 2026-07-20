"""Small, self-contained copies of ARC helpers used by the adapter.

These are faithful ports of the ARC originals, copied so ``tckdb_arc`` never
needs to import ARC for the pure-dict payload paths. Sources:

* ``E_h_kJmol``                        ← ``arc/constants.py``
* ``read_yaml_file``                   ← ``arc/common.py``
* element dicts / ``get_most_common_isotope_for_element``
  / ``xyz_from_data`` / ``check_xyz_dict`` / ``xyz_to_str``
                                       ← ``arc/species/converter.py`` (+ ``arc/common.py``)

Deviations from ARC (documented on purpose, behavior-preserving for the
adapter's inputs, which are always ARC xyz *dicts* read from ``output.yml``):

* ``read_yaml_file`` drops the ``project_directory`` path-globalization branch
  (ARC-only ``globalize_paths``); the adapter/sweep/cli only ever pass an
  absolute ``path``.
* ``check_xyz_dict`` keeps only the dict input branch. ARC also accepts a
  string xyz or a Z-matrix and converts them (``str_to_xyz`` / ``zmat_to_xyz``);
  the adapter never passes those, so a non-dict input raises ``ConverterError``
  instead of dragging in ARC's coordinate-conversion machinery.

The ``kabsch`` RMSD helper is NOT vendored here — it pulls ARC's full
center-of-mass / atomic-mass subsystem (and scipy). It is only used on the
GSM path-search path, which already degrades through ``_arc_optional``; so
``kabsch`` is routed there (see ``_arc_optional.kabsch``).
"""

from __future__ import annotations

import os

import numpy as np
import yaml


# --- constants (arc/constants.py) -----------------------------------------
#: The Hartree energy E_h in J.
E_h = 4.35974434e-18
#: The Avogadro constant N_A in mol^-1.
Na = 6.02214179e23
#: The Hartree energy in kJ/mol (1 Hartree = 2625.5 kJ/mol).
E_h_kJmol = E_h * Na / 1000


# --- exceptions (arc/exceptions.py) ---------------------------------------
class ConverterError(Exception):
    """An exception raised when converting molecular representations fails."""


# --- YAML I/O (arc/common.py) ---------------------------------------------
def read_yaml_file(path: str, project_directory: str | None = None) -> dict | list:
    """Read a YAML file and return the parsed Python object.

    Faithful port of ``arc.common.read_yaml_file`` minus the ARC-only
    ``project_directory`` globalization (unused by the adapter/sweep/cli).

    Args:
        path (str): The YAML file path to read.
        project_directory (str, optional): Accepted for signature parity; the
            standalone vendor does not rebase paths and ignores it.

    Returns: dict | list
        The content read from the file.
    """
    if not isinstance(path, str):
        raise ValueError(f"path must be a string, got {path} which is a {type(path)}")
    if not os.path.isfile(path):
        raise ValueError(f"Could not find the YAML file {path}")
    with open(path, "r") as f:
        content = yaml.load(stream=f, Loader=yaml.FullLoader)
    return content


# --- element dictionaries (arc/common.py::read_element_dicts) -------------
_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _read_element_dicts() -> tuple[dict, dict, dict]:
    """Read the bundled ``data/elements.yml`` into (symbol_by_number,
    number_by_symbol, mass_by_symbol). Faithful port of ARC's
    ``read_element_dicts`` (covalent radii omitted — unused here)."""
    elements_path = os.path.join(_DATA_DIR, "elements.yml")
    contents = read_yaml_file(elements_path)
    symbol_by_number = contents["symbol_by_number"]
    number_by_symbol = {value: key for key, value in symbol_by_number.items()}
    mass_by_symbol = contents["mass_by_symbol"]
    return symbol_by_number, number_by_symbol, mass_by_symbol


SYMBOL_BY_NUMBER, NUMBER_BY_SYMBOL, MASS_BY_SYMBOL = _read_element_dicts()


# --- isotope helper (arc/species/converter.py) ----------------------------
def get_most_common_isotope_for_element(element_symbol):
    """Get the most common isotope for a given element symbol.

    Returns ``None`` for dummy atoms ('X'). Faithful port.
    """
    if element_symbol == "X":
        # this is a dummy atom (such as in a zmat)
        return None
    mass_list = MASS_BY_SYMBOL[element_symbol]
    if len(mass_list[0]) == 2:
        # isotope contribution is unavailable, just get the first entry
        isotope = mass_list[0][0]
    else:
        # get the most common isotope
        isotope, isotope_contribution = mass_list[0][0], mass_list[0][2]
        for iso in mass_list:
            if iso[2] > isotope_contribution:
                isotope_contribution = iso[2]
                isotope = iso[0]
    return isotope


# --- xyz builders (arc/species/converter.py) ------------------------------
def xyz_from_data(coords, numbers=None, symbols=None, isotopes=None) -> dict:
    """Get the ARC xyz dictionary format from raw data. Faithful port."""
    if isinstance(coords, np.ndarray):
        coords = tuple(tuple(coord.tolist()) for coord in coords)
    elif isinstance(coords, list):
        coords = tuple(tuple(coord) for coord in coords)
    if numbers is not None and isinstance(numbers, np.ndarray):
        numbers = tuple(numbers.tolist())
    elif numbers is not None and isinstance(numbers, list):
        numbers = tuple(numbers)
    if symbols is not None and isinstance(symbols, list):
        symbols = tuple(symbols)
    if isotopes is not None and isinstance(isotopes, list):
        isotopes = tuple(isotopes)
    if not isinstance(coords, tuple):
        raise ConverterError("Expected coords to be a tuple, got {0} which is a {1}".format(coords, type(coords)))
    if numbers is not None and not isinstance(numbers, tuple):
        raise ConverterError("Expected numbers to be a tuple, got {0} which is a {1}".format(numbers, type(numbers)))
    if symbols is not None and not isinstance(symbols, tuple):
        raise ConverterError("Expected symbols to be a tuple, got {0} which is a {1}".format(symbols, type(symbols)))
    if isotopes is not None and not isinstance(isotopes, tuple):
        raise ConverterError("Expected isotopes to be a tuple, got {0} which is a {1}".format(isotopes, type(isotopes)))
    if numbers is None and symbols is None:
        raise ConverterError('Must set either "numbers" or "symbols". Got neither.')
    if numbers is not None and symbols is not None:
        raise ConverterError('Must set either "numbers" or "symbols". Got both.')
    if numbers is not None:
        symbols = tuple(SYMBOL_BY_NUMBER[number] for number in numbers)
    if len(coords) != len(symbols):
        raise ConverterError(f"The length of the coordinates ({len(coords)}) is different than the length of the "
                             f"numbers/symbols ({len(symbols)}).")
    if isotopes is not None and len(coords) != len(isotopes):
        raise ConverterError(f"The length of the coordinates ({len(coords)}) is different than the length of isotopes "
                             f"({len(isotopes)}).")
    if isotopes is None:
        isotopes = tuple(get_most_common_isotope_for_element(symbol) for symbol in symbols)
    xyz_dict = {"symbols": symbols, "isotopes": isotopes, "coords": coords}
    return xyz_dict


def check_xyz_dict(xyz, project_directory: str | None = None):
    """Check that the xyz dictionary entered is valid.

    Faithful port of ARC's ``check_xyz_dict`` restricted to the dict input
    branch (the only shape the adapter passes). String / Z-matrix inputs are
    rejected rather than converted (see module docstring).
    """
    if xyz is None:
        return None
    if not isinstance(xyz, dict):
        raise ConverterError(
            f"Expected an ARC xyz dictionary, got {type(xyz)}. The standalone vendor does not "
            f"convert string / Z-matrix inputs."
        )
    xyz_dict = xyz
    if "vars" in list(xyz_dict.keys()):
        raise ConverterError(
            "Got a Z-matrix (has 'vars'); the standalone vendor does not convert Z-matrices."
        )
    if "symbols" not in list(xyz_dict.keys()):
        raise ConverterError(f"XYZ dictionary is missing symbols. Got:\n{xyz_dict}")
    if "coords" not in list(xyz_dict.keys()):
        raise ConverterError(f"XYZ dictionary is missing coords. Got:\n{xyz_dict}")
    if len(xyz_dict["symbols"]) != len(xyz_dict["coords"]):
        raise ConverterError(f'Got {len(xyz_dict["symbols"])} symbols and {len(xyz_dict["coords"])} '
                             f"coordinates:\n{xyz_dict}")
    xyz_dict = xyz_from_data(coords=xyz_dict["coords"],
                             symbols=xyz_dict["symbols"],
                             isotopes=xyz_dict["isotopes"] if "isotopes" in list(xyz_dict.keys()) else None)
    if len(xyz_dict["symbols"]) != len(xyz_dict["isotopes"]):
        raise ConverterError(f'Got {len(xyz_dict["symbols"])} symbols and {len(xyz_dict["isotopes"])} '
                             f"isotopes:\n{xyz_dict}")
    return xyz_dict


def xyz_to_str(xyz_dict: dict, isotope_format: str | None = None) -> str | None:
    """Convert an ARC xyz dictionary to a string xyz format. Faithful port."""
    if xyz_dict is None:
        return None
    xyz_dict = check_xyz_dict(xyz_dict)
    recognized_isotope_formats = ["gaussian"]
    if any([key not in list(xyz_dict.keys()) for key in ["symbols", "isotopes", "coords"]]):
        raise ConverterError(f'Missing keys in the xyz dictionary. Expected to find "symbols", "isotopes", and '
                             f'"coords", but got {list(xyz_dict.keys())} in\n{xyz_dict}')
    if any([len(xyz_dict["isotopes"]) != len(xyz_dict["symbols"]),
            len(xyz_dict["coords"]) != len(xyz_dict["symbols"])]):
        raise ConverterError(f'Got different lengths for "symbols", "isotopes", and "coords": '
                             f'{len(xyz_dict["symbols"])}, {len(xyz_dict["isotopes"])}, and {len(xyz_dict["coords"])}, '
                             f"respectively, in xyz:\n{xyz_dict}")
    if any([len(xyz_dict["coords"][i]) != 3 for i in range(len(xyz_dict["coords"]))]):
        raise ConverterError(f"Expected 3 coordinates for each atom (x, y, and z), got:\n{xyz_dict}")
    xyz_list = list()
    for symbol, isotope, coord in zip(xyz_dict["symbols"], xyz_dict["isotopes"], xyz_dict["coords"]):
        common_isotope = get_most_common_isotope_for_element(symbol)
        if isotope_format is not None and common_isotope != isotope:
            # consider the isotope number
            if isotope_format == "gaussian":
                element_with_isotope = "{0}(Iso={1})".format(symbol, isotope)
                row = "{0:14}".format(element_with_isotope)
            else:
                raise ConverterError("Recognized isotope formats for printing are {0}, got: {1}".format(
                                      recognized_isotope_formats, isotope_format))
        else:
            # don't consider the isotope number
            row = "{0:4}".format(symbol)
        row += "{0:14.8f}{1:14.8f}{2:14.8f}".format(*coord)
        xyz_list.append(row)
    return "\n".join(xyz_list)
