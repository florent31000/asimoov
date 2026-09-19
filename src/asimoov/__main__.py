"""ASIMOOV CLI entry point.

Placeholder owned by WS0 so `pip install -e .` and the `asimoov` console
script resolve. WS1 (core) replaces this with the real command dispatch:
run|face|enroll|replay|doctor|stats|mcp. See plan.md section 4.12 (WS1).
"""

import sys


def main(argv: list[str] | None = None) -> int:
    print("asimoov: core runtime not implemented yet (WS1).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
