"""ASIMOOV contracts: the frozen vocabulary and ABCs every workstream builds against.

Zero third-party dependencies (pyyaml, used only by `persona.py`, is the one
documented exception; `jsonschema` ships as a core dependency but is
imported only by tests and tooling, never by this package). See
``CONTRACTS_FROZEN.md`` at the repo root: changes here are additive only
(v1 -> v1.1) and go through WS0.
"""
