# voice (WS2)

`VoiceProvider` implementation for OpenAI Realtime (asyncio over `websockets`),
`SessionManager`, `PlaybackTracker`, desktop/Android capture and playback, AEC,
Silero-based barge-in, resampling.

Owned by WS2. Implements the `asimoov.contracts.voice.VoiceProvider` and
`asimoov.contracts.audio` ABCs (frozen). See `plan.md` section 4.12, row WS2,
and section 4.4 (concurrency, barge-in, session renewal).
