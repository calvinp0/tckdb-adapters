"""Pytest configuration for the tckdb_core test suite.

Puts the tests directory on ``sys.path`` so helper imports (``_backend_import``)
resolve regardless of the invocation cwd.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
