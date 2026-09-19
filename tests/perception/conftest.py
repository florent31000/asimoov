"""Schema validators, and the path fix the shared stubs need.

pytest's importlib import mode does not put the test directory on `sys.path`,
so `helpers.py` would not be importable by name without this.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

SCHEMAS = HERE.parents[1] / "src" / "asimoov" / "contracts" / "schemas"


@pytest.fixture(scope="session")
def percept_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS / "percept.v1.json").read_text()))


@pytest.fixture(scope="session")
def envelope_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS / "envelope.v1.json").read_text()))
