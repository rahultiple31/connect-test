"""TTS utilities: text splitting, SSML formatting, and hold message selection."""

from __future__ import annotations

import random
import re


_DEFAULT_HOLD_MESSAGE = "Please hold while I look into that for you."


def split_text(text: str, max_length: int = 3000) -> list[str]:
    """Split *text* into segments of at most *max_length* characters.

    Splits prefer sentence boundaries (`.` `!` `?` followed by a space or
    end-of-string).  When no sentence boundary exists within the limit the
    text is force-split at *max_length*.

    Concatenating the returned segments reproduces the original text exactly.
    """
    if len(text) <= max_length:
        return [text]

    segments: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            segments.append(remaining)
            break

        chunk = remaining[:max_length]
        # Find the last sentence boundary within the chunk.
        # Sentence-ending punctuation followed by a space or at the very end.
        match = None
        for m in re.finditer(r"[.!?](?:\s|$)", chunk):
            match = m

        if match:
            # Include the punctuation character; if followed by a space the
            # space stays at the start of the next segment so concat is exact.
            split_pos = match.start() + 1  # after the punctuation char
            segments.append(remaining[:split_pos])
            remaining = remaining[split_pos:]
        else:
            # No sentence boundary — force split.
            segments.append(remaining[:max_length])
            remaining = remaining[max_length:]

    return segments


def _escape_xml(text: str) -> str:
    """Escape XML special characters in *text*."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def format_ssml(segments: list[str], pause_ms: int = 1000) -> str:
    """Wrap *segments* in SSML ``<speak>`` tags with ``<break>`` pauses.

    Special XML characters inside each segment are escaped.  A single segment
    produces ``<speak>{text}</speak>``.  Multiple segments are joined with
    ``<break time="{pause_ms}ms"/>`` inside one ``<speak>`` element.
    """
    escaped = [_escape_xml(s) for s in segments]
    break_tag = f'<break time="{pause_ms}ms"/>'
    return f"<speak>{break_tag.join(escaped)}</speak>"


def select_hold_message(
    pool: list[str],
    last_message: str | None = None,
) -> str:
    """Pick a hold message from *pool*, avoiding *last_message*.

    * If *pool* is empty, returns a built-in default message.
    * If *pool* has one entry, returns it regardless of *last_message*.
    * Otherwise, randomly selects from *pool* excluding *last_message*.
    """
    if not pool:
        return _DEFAULT_HOLD_MESSAGE

    if len(pool) == 1:
        return pool[0]

    candidates = [m for m in pool if m != last_message]
    return random.choice(candidates)
