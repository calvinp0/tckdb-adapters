"""Small, self-contained copies of ARC helpers used by the adapter.

These are faithful ports of the ARC originals, copied so ``tckdb_arc`` never
needs to import ARC for the pure-dict payload paths. The element data, the xyz
helpers and the energy constant are producer-agnostic and moved to ``tckdb_core``
in batch L2 (:mod:`tckdb_core.xyz`, :mod:`tckdb_core.physical_constants`); they
are re-exported here so ``tckdb_arc._vendor.<name>`` keeps resolving. What stays
vendored in this module is ``read_yaml_file``:

* ``read_yaml_file``                   <- ``arc/common.py``

Deviation from ARC (documented on purpose): ``read_yaml_file`` drops the
``project_directory`` path-globalization branch (ARC-only ``globalize_paths``);
the adapter/sweep/cli only ever pass an absolute ``path``.

The ``kabsch`` RMSD helper is NOT vendored here — it pulls ARC's full
center-of-mass / atomic-mass subsystem (and scipy). It is only used on the
GSM path-search path, which already degrades through ``_arc_optional``; so
``kabsch`` is routed there (see ``_arc_optional.kabsch``).
"""

from __future__ import annotations

import os

import yaml

from tckdb_core.physical_constants import E_h, E_h_kJmol, Na  # noqa: F401
from tckdb_core.xyz import (  # noqa: F401
    MASS_BY_SYMBOL,
    NUMBER_BY_SYMBOL,
    SYMBOL_BY_NUMBER,
    ConverterError,
    check_xyz_dict,
    get_most_common_isotope_for_element,
    xyz_from_data,
    xyz_to_str,
)


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
