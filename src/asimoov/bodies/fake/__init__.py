"""The `fake` body: entry point target for `asimoov.bodies:fake`.

`manifest.yaml` in this directory documents the same body in body.v1 YAML
form; `FakeBody` (in `asimoov.contracts.fakes`, owned by WS0) builds its
default manifest directly in Python so contract tests never depend on YAML
loading. Re-exported here so it can be loaded like any other body plugin.
"""

from asimoov.contracts.fakes import FakeBody

__all__ = ["FakeBody"]
