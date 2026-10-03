#!/usr/bin/env python3
"""Regenerate ``docs/contract/WARNING_CODES.md`` from the warning-code registries.

The registries are ``tckdb_arc.warning_codes.ArcWarning`` and
``tckdb_core.warning_codes.CoreWarning``. ``tckdb_arc/tests/test_warning_registry.py`` fails
when the committed document differs from what this writes::

    python tools/gen_warning_codes.py          # rewrite the document
    python tools/gen_warning_codes.py --check  # exit 1 when it is stale
"""

import argparse
import sys
from pathlib import Path

from tckdb_arc.warning_codes import DOC_RELATIVE_PATH, render_markdown


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when the document is stale instead of writing it")
    args = parser.parse_args(argv)
    path = Path(__file__).resolve().parents[1] / DOC_RELATIVE_PATH
    text = render_markdown()
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            print(f"{path} is stale; run: python tools/gen_warning_codes.py", file=sys.stderr)
            return 1
        return 0
    path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
