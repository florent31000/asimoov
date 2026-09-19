# Contributing

ASIMOOV is Apache-2.0 and built in the open. Open an issue before a large
pull request; small fixes can go straight to a PR.

```bash
git clone https://github.com/florent31000/asimoov
cd asimoov
python -m pip install -e ".[dev]"
pytest
ruff check .
```

Both must be green before you submit. CI runs them on Python 3.10 and 3.12.

## Adding a body adapter

A body is one class and one manifest. Nothing else in ASIMOOV needs to know
your robot exists.

**1. Write the manifest.** `BodyManifest` (`asimoov.contracts.body`) declares
what the body can do: its kind, its capabilities, the gestures it supports,
its safety limits. Every value comes from `asimoov.contracts.vocab` — if the
vocabulary has no word for what your robot does, open an issue rather than
inventing one locally.

**2. Implement the `Body` ABC.** Nine methods: `start`, `stop`, `manifest`,
`gesture`, `look_at`, `stop_all`, `health`, plus the optional
`audio_source()` / `audio_sink()` when the body carries a microphone or a
speaker. Read the docstrings: they state the timings and the exceptions, and
they are the contract.

Three rules that are not negotiable:

- Every call to the robot has a timeout. A command that can hang is a bug,
  not an edge case.
- `stop_all()` stops everything, immediately, from any state.
- Losing the link stops the robot. Declare
  `manifest.safety.stop_on_disconnect` and implement the optional
  `simulate_disconnect()` so the conformance suite can prove it.

**3. Pass the conformance suite.**

```python
from asimoov.bodies.conformance import run_conformance

run_conformance(MyBody(...))
```

It checks the manifest against the frozen vocabularies, that every declared
gesture is callable, that long behaviours answer asynchronously, that
`stop_all` is honoured, and that a disconnect stops the body within a
second.

**4. Register it.** Add an entry point under `asimoov.bodies` and a
`robots/<name>/robot.yaml`. Adding a dependency means an extra in
`pyproject.toml`, which is an issue, not a direct edit.

**5. Say how you tested it.** A body adapter is judged on a real robot. Say
which one, which firmware, and what you ran.

## Adding a personality

Write a `persona.yaml`, run it, publish it, tell us. Personas are data:
no code review needed, just a robot we can meet.

## Code style

- Code, comments, docs and commit messages in English.
- `ruff check .` with the repo settings; 100 columns.
- Small files. No speculative abstraction, no configuration knob nobody
  asked for, no comment that restates the line below it.
- Docstrings say what a caller needs: timings, exceptions, units,
  conventions. They do not paraphrase the code.
- No silent fallback. A `try/except` that swallows an error, a `return {}`
  that hides a missing case, or a default that papers over a misconfiguration
  is worse than the crash. Fail with the reason.
- New behaviour comes with a test. `contracts/fakes.py` has a fake body,
  voice provider, face renderer and memory store, and
  `contracts/examples/` has one frozen example per schema.

## The contracts are frozen

`src/asimoov/contracts/**`, `src/asimoov/bodies/conformance.py` and the
shared fakes are frozen (`CONTRACTS_FROZEN.md`). Changes are additive only —
a new optional field with a default, a new vocabulary value, a new optional
method with a no-op default. Renaming a field, removing a value or changing
an existing signature is a new contracts version and goes through an issue.

## No secrets

Never commit or print an API key, a token or a credential. Runtime secrets
live outside the repository: `~/.asimoov/secrets.yaml` on a desktop,
`getFilesDir()/secrets.yaml` on Android, never in the APK. If you think you
have committed one, say so immediately — rotating a key is cheap, a leaked
key in git history is not.

Face data is personal data. Test fixtures are synthetic; never commit a real
face, a real recording, or a memory database from a real household.

## License

By contributing you agree your work is released under Apache 2.0
([LICENSE](LICENSE)).
