"""Episode summaries, written when a conversation ends.

The summarizer does not know about any LLM: it is given a text callable
(WS2's voice provider can produce one with an out-of-band
``response.create``, plan.md section 4.4). Without one it falls back to an
explicitly extractive summary -- the last lines, truncated -- and says so
in the returned text rather than pretending a model wrote it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 400
EXTRACTIVE_LINES = 4
SUMMARY_PROMPT = (
    "Summarize this conversation in two sentences, in the language it was held in. "
    "Mention who took part and what mattered to them.\n\n"
)

TextCallable = Callable[[str], Awaitable[str]]


class EpisodeSummarizer:
    """Turns a transcript into the one-paragraph summary stored on an episode."""

    def __init__(self, generate: TextCallable | None = None) -> None:
        self.generate = generate

    async def summarize(self, transcript: list[str]) -> str:
        """Summarize ``transcript``; never raises, always returns usable text."""
        joined = "\n".join(line.strip() for line in transcript if line.strip())
        if not joined:
            return ""
        if self.generate is None:
            return self._extractive(joined)
        try:
            summary = await self.generate(SUMMARY_PROMPT + joined)
        except Exception:
            log.exception("episode summarizer failed, storing an extractive summary instead")
            return self._extractive(joined)
        summary = (summary or "").strip()
        return summary[:SUMMARY_MAX_CHARS] if summary else self._extractive(joined)

    @staticmethod
    def _extractive(joined: str) -> str:
        tail = joined.splitlines()[-EXTRACTIVE_LINES:]
        return ("(extract) " + " / ".join(tail))[:SUMMARY_MAX_CHARS]
