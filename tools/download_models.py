#!/usr/bin/env python3
"""Download and verify the perception ONNX models into ~/.asimoov/models/.

Same code path as `asimoov doctor --download-models`, usable before the core
CLI exists:

    python tools/download_models.py [--force] [--no-optional]
"""

from __future__ import annotations

import argparse
import sys

from asimoov.perception import PerceptionError
from asimoov.perception.models import MODELS, download_models, models_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument("--no-optional", action="store_true", help="skip the Silero VAD model")
    args = parser.parse_args(argv)

    print(f"models directory: {models_dir()}")
    for spec in MODELS:
        print(f"  {spec.name:22s} {spec.filename}{' (optional)' if spec.optional else ''}")
    try:
        paths = download_models(include_optional=not args.no_optional, force=args.force)
    except PerceptionError as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        return 1
    for path in paths:
        print(f"ok {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
