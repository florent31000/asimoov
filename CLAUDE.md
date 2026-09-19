# ASIMOOV -- agent instructions

This is the public repository of ASIMOOV: the open-source soul for
companion robots. Apache 2.0. Code and docs in English.

## Source of truth

The full plan lives in `../asimoov-private/plan.md` (French, private,
never copied into this repo). Read it before starting any workstream; it
is binding, not a suggestion. Sections 5 and 6 (business, marketing) stay
in `asimoov-private/` and never leak into this repo, its commit history,
or its issues.

## Workstream ownership

The plan (section 4.12) splits the repo into disjoint workstreams (WS0-WS8),
each owning specific directories exclusively. Before touching a file,
check which workstream owns its directory (see the table in the plan, and
the `README.md` placeholder left in every directory not yet implemented).
Do not edit another workstream's files. `pyproject.toml` is owned by WS0
only; adding a dependency anywhere else is an issue, not a direct edit.

## Contracts are frozen

`src/asimoov/contracts/**`, `src/asimoov/bodies/conformance.py`, and
`src/asimoov/bodies/fake/**` are frozen at v1.3 (`CONTRACTS_FROZEN.md`).
Changes are additive only (a new optional field, a new enum value) and go
through WS0. Never modify an existing ABC signature, remove a vocabulary
value, or add a dependency to `contracts/` (stdlib only, plus `pyyaml` in
`persona.py` and `jsonschema` in tests).

## No secrets

Never commit or print an API key, token, or credential. The real OpenAI
key that leaked into `neon/config/settings.yaml` and the Neon APKs must be
revoked by Florent before any code derived from Neon is published; never
read that file. Android secrets belong in `getFilesDir()/secrets.yaml` at
runtime, never in the APK or the repo.

## Git

Agents never run `git commit`, `git push`, or `git add`. Florent manages
his own git workflow.

## Simplicity first

Build exactly what the plan specifies for your workstream. No speculative
abstractions, no extra configuration knobs, no narrative comments that
restate the code. Small files. If a design decision in the plan seems
wrong, flag it in your report; do not silently deviate.

## Forbidden dependencies in the core install

Per plan.md section 4.10, none of the following may be imported by
anything installed without an extra (i.e. by `pip install asimoov` alone):
`pydantic`, `onnxruntime`, `opencv`, `scipy`, `aiohttp`, `msgspec`,
`orjson`, `uvloop`, `torch`. Allowed in core: stdlib, `websockets`,
`pyyaml`, `numpy`, `jsonschema`, `certifi`. The `[go2]` extra's dependency
list is proven by Neon's `buildozer.spec` and must not be changed without
re-verifying it builds for Android.
