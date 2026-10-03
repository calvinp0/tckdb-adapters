"""Producer-agnostic building blocks for TCKDB upload adapters.

A producer adapter (ARC today, others to follow) reads its tool's output and
builds TCKDB payloads; everything after that, and the helpers every producer
shares, lives here. See the package README for what belongs in this package.
"""

__version__ = "0.2.0"
