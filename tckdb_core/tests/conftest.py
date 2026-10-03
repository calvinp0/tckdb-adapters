"""Pytest configuration for the tckdb_core test suite.

Puts the tests directory on ``sys.path`` so helper imports
resolve regardless of the invocation cwd, and registers the contract hook's marker.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from tckdb_core.testing.contract_hook import register_markers  # noqa: E402


def pytest_configure(config):
    register_markers(config)
