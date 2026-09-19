# tests/voice (WS2)

Fake Realtime server: real tool call round-trip, `response.cancel` with
`response_id`, `conversation.item.truncate` on barge-in, session renewal
without loss, no mic chunk sent while `remaining_ms > 0`. See `plan.md`
section 4.12, row WS2.
