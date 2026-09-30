"""A synthetic mini-repo for the drift tool's tests.

The tests must not depend on the checkout's real pins: the drift workflow bumps the
real files to the newest TCKDB before it runs the suite. Everything here uses fixed
values (schemas 0.54 / client 0.95, adapter 1.2.3).
"""

from pathlib import Path

PIN_REPO = "https://example.org/tckdb.git"
OLD_SHA = "a" * 40
PINNED_SCHEMAS = (0, 54)
PINNED_CLIENT = (0, 95)
ADAPTER_VERSION = "1.2.3"

FILES = {
    "tckdb_arc/pyproject.toml": f'''[project]
name = "tckdb-arc"
version = "{ADAPTER_VERSION}"
dependencies = [
    "tckdb-client>=0.95,<0.96",   # transport
    "tckdb-schemas>=0.54,<0.55", # contract
    "PyYAML>=6",
]
''',
    "tckdb_arc/tests/_contract.py": '"""doc"""\n\nTARGET_SCHEMAS_LINE = (0, 54)\nOTHER = 1\n',
    "tckdb-pin.toml": f'''# pin
[tckdb]
repo = "{PIN_REPO}"
sha = "{OLD_SHA}"
schemas_subdir = "schemas/python/tckdb-schemas"
client_subdir = "clients/python"
''',
    "README.md": f'''# adapters

The tested contract is `tckdb-client` 0.95.x with `tckdb-schemas` 0.54.x
(notes).

```bash
pip install \\
  "tckdb-client @ git+{PIN_REPO}@{OLD_SHA}#subdirectory=clients/python" \\
  "tckdb-schemas @ git+{PIN_REPO}@{OLD_SHA}#subdirectory=schemas/python/tckdb-schemas"
```
''',
    "tckdb_arc/README.md": '''# tckdb-arc

Requires Python 3.11+, `tckdb-client` 0.95.x and `tckdb-schemas` 0.54.x.

Tracked TCKDB releases:

<!-- tckdb-drift:changelog -->

Other text.
''',
    ".github/workflows/ci.yml": "name: ci\n",
    ".github/workflows/tckdb-drift.yml": "name: drift\n",
    "CLAUDE.md": "# rules\n",
}

#: The files a bump rewrites; the rest of FILES must stay byte-identical.
BUMPED = [
    "tckdb_arc/pyproject.toml",
    "tckdb_arc/tests/_contract.py",
    "tckdb-pin.toml",
    "README.md",
    "tckdb_arc/README.md",
]


def build(root: Path) -> Path:
    for rel, text in FILES.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    return root
