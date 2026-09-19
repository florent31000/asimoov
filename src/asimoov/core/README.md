# core (WS1)

Local bus + WebSocket hub, `SocialScene`/attention, `Mind` (injector, initiative),
`BehaviorResolver`/`Executor`, `ToolRegistry` + MCP export, SQLite `MemoryStore`,
`SafetyGuard`, config, logging, telemetry, replay, CLI (`asimoov.__main__`).

How it fits together, and how the other workstreams plug in:
[`docs/architecture.md`](../../../docs/architecture.md).

Owned by WS1. Depends on `asimoov.contracts` (frozen, see `CONTRACTS_FROZEN.md`).
