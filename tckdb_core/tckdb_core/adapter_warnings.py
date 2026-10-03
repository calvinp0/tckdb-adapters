"""Typed adapter warnings and the sink that stores them in the sidecar shape.

An adapter warning is a producer-side self-check finding: something the adapter
saw in the producer's output and chose to flag (an omitted block, a refused
claim, an assumed value). It travels in the sidecar's ``warnings`` list next to
the server's own findings, so it has the server's shape::

    {"code": ..., "message": ..., "field": ...,
     "context": {"source": "<producer>_self_check", "action": ..., ...}}

``source`` names the producer, so a reader of the sidecar can tell the adapter's
findings from the server's. The producer tag is a parameter (the ARC adapter
passes its own package name), never defaulted here, because the shared code does not know
which producer it serves.

The code table (every code an adapter can emit, with its meaning) is not here
yet; each producer still defines its own ``_W_*`` constants.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping


@dataclasses.dataclass(frozen=True)
class AdapterWarning:
    """One self-check finding.

    ``context`` carries the finding's own details (``action``, ids, ...); the
    ``source`` entry is added when the warning is rendered, ahead of them.
    """

    code: str
    message: str
    field: str | None = None
    context: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self, producer: str) -> dict[str, Any]:
        """The sidecar dict: ``source`` first, then the context entries in order."""
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "context": {"source": f"{producer}_self_check", **self.context},
        }


class WarningSink(list):
    """A list of sidecar warning dicts that renders :class:`AdapterWarning` on entry.

    ``append`` and ``add`` both store the dict the sidecar holds today, so a sink
    can be handed anywhere a ``list[dict]`` of warnings is expected. A plain dict
    passes through unchanged (so code that still builds its own dicts can share
    the sink while it is converted).
    """

    def __init__(self, producer: str, items: Iterable[Any] = ()):
        super().__init__()
        self.producer = producer
        for item in items:
            self.append(item)

    def append(self, warning: AdapterWarning | Mapping[str, Any]) -> None:  # type: ignore[override]
        if isinstance(warning, AdapterWarning):
            warning = warning.to_dict(self.producer)
        super().append(warning)

    def add(
        self,
        code: str,
        message: str,
        field: str | None = None,
        **context: Any,
    ) -> dict[str, Any]:
        """Build, store and return the sidecar dict for one finding."""
        rendered = AdapterWarning(code, message, field, context).to_dict(self.producer)
        super().append(rendered)
        return rendered
