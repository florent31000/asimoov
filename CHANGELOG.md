# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- contracts v1.1 -- 2026-09-19: `contracts/bus.py` (`Bus` protocol,
  `Subscription`), `BodyContext.bus` / `PerceptionContext.bus` typed as
  `Bus | None`; `vocab.TOPICS` and `vocab.APP_PERMISSIONS`; `app.v1.json`
  `permissions` enum; frozen sign conventions for `bearing.az` /
  `GazeTarget.az` / `FaceGaze.x` (docstrings corrected, documented in
  `docs/contracts.md`); audio fakes (`FakeAudioSource`, `FakeAudioSink`,
  `FakePlaybackTracker`), wired into `FakeBody`; optional
  `simulate_disconnect()` conformance check; `robots/avatar/behaviors/`
  and `robots/README.md`.
- Repository skeleton and frozen contracts v1 (`src/asimoov/contracts/`):
  vocabularies, ABCs (`Body`, `FaceRenderer`, `VoiceProvider`,
  `PerceptionModule`, `MemoryStore`), 7 JSON Schemas with examples, shared
  fakes, `Body` conformance suite, `pyproject.toml`, CI.
