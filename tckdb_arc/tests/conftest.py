"""Pytest configuration for the tckdb_arc test suite.

Puts the tests directory on ``sys.path`` so cross-test helper imports
(``from test_adapter import _reaction_output_doc``) resolve regardless of the
invocation cwd — mirroring the Chemkin adapter's conftest convenience.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
