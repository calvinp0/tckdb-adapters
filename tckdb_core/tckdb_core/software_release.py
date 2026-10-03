"""Software-release version handling: split an ESS banner as TCKDB would.

TCKDB keeps a program's version and revision in separate columns and normalises a
composite ``version`` banner itself. Splitting before sending states exactly what
the server would store.
"""

from __future__ import annotations

import re

from tckdb_schemas.fragments.refs import (
    W_SOFTWARE_RELEASE_VERSION_IS_COMPOSITE,
    SoftwareReleaseRef,
)

XTB_BANNER_RE = re.compile(r"^\s*xtb\s+version\s+(?P<version>\S+)(?:\s+\((?P<build>[0-9A-Za-z]+)\))?", re.IGNORECASE)


def split_version_banner(name: str, banner: str) -> dict[str, str]:
    """Split an ESS banner into ``version`` (and ``revision``) as TCKDB would.

    A producer records the banner as printed (``'Gaussian 16,
    Revision C.02'``). TCKDB keeps version and revision in separate columns
    and, given the banner as ``version``, normalises it itself with a
    ``software_release_version_is_composite`` warning. The rule is the shared
    ``tckdb_schemas.fragments.refs.SoftwareReleaseRef.normalize_composite_version``
    (the model the server validates with), so it is reused here rather than
    re-implemented and the producer sends exactly what the server would store:

    * no internal whitespace (``'16'``, ``'5.0.4'``): unchanged;
    * leading token equal to ``name`` (case-insensitive): stripped, and a
      trailing ``', Revision <label>'`` split into ``revision``
      (``'Gaussian 16, Revision C.02'`` -> ``16`` / ``C.02``, ``'ORCA 5.0.4'``
      -> ``5.0.4``, ``'Molpro 2022.3'`` -> ``2022.3``);
    * leading token naming another program (``name='gaussian'``,
      ``'ORCA 6.0.0'``; ``name='qchem'``, ``'Q-Chem 5.4'``): unchanged, as
      the server leaves it (it warns ``software_release_name_looks_wrong``);
      the producer never guesses which of the two is right.

    The server's ``[auto] ...`` provenance note is not sent: the split is
    a deterministic reformat, and the banner is reconstructible from
    ``name``/``version``/``revision``.
    """
    unchanged = {"version": banner}
    xtb = XTB_BANNER_RE.match(banner) if str(name).lower() == "xtb" else None
    if xtb is not None:
        # xtb prints ``xtb version 6.7.1 (edcfbbe) compiled by ...``: the shared
        # composite rule would keep the word "version" in ``version``. The release
        # number is the version and the parenthesised hash its build.
        split = {"version": xtb.group("version")}
        if xtb.group("build"):
            split["build"] = xtb.group("build")
        return split
    try:
        ref = SoftwareReleaseRef(name=name, version=banner)
    except ValueError:
        return unchanged
    warning = ref.version_warning()
    if warning is None or warning.code != W_SOFTWARE_RELEASE_VERSION_IS_COMPOSITE:
        return unchanged
    split = {"version": ref.version}
    if ref.revision is not None:
        split["revision"] = ref.revision
    return split
