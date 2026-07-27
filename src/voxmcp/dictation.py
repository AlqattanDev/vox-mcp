"""Tidy a dictated transcript before it is typed into someone else's app.

Whisper already emits casing and punctuation, so this is polish, not
reconstruction: strip the annotations it writes about the audio, drop the
filler words nobody means to type, and make sure the result reads as a
sentence.  Everything here is a local regex — no model, no network, and no
rewriting of what was actually said.
"""

from __future__ import annotations

import re

from .intents import is_non_speech_transcript


# whisper.cpp annotates ambient sound rather than returning nothing:
# [BLANK_AUDIO], [MUSIC], (door closes). Those are labels about the recording,
# never words to type.
_MARKER = re.compile(r"[\[(][^\])]*[\])]")

# Spoken hesitation, English and the Gulf Arabic equivalents. Only matched as
# whole words, and only where they stand alone — "um" inside "umbrella" and a
# deliberate "yaani" mid-sentence both survive.
_FILLERS = (
    "uh",
    "uhh",
    "um",
    "umm",
    "erm",
    "hmm",
    "mhm",
    "uhm",
    "er",
    "ah",
    "أه",
    "اه",
    "ااه",
    "امم",
    "همم",
    "يعني",
)
_FILLER = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(word) for word in _FILLERS) + r")(?!\w)[,]?",
    flags=re.IGNORECASE | re.UNICODE,
)

_WHITESPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?؟،])")
_TERMINAL = ".!?…؟"

# Arabic and other non-cased scripts have no capital form; forcing .upper() on
# them is a no-op at best, so only touch text that actually starts with a
# cased letter.
_CASED_START = re.compile(r"^[a-z]")


def clean_dictation(text: str | None, *, mode: str = "rules") -> str:
    """Return ``text`` ready to paste, or an empty string if nothing was said.

    ``mode="off"`` disables everything except the non-speech check, for when
    the raw transcript is wanted verbatim.
    """

    if not isinstance(text, str):
        return ""
    if is_non_speech_transcript(text):
        return ""
    if mode == "off":
        return text.strip()

    cleaned = _MARKER.sub(" ", text)
    cleaned = _FILLER.sub(" ", cleaned)
    cleaned = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    # Stripping fillers can leave a sentence starting on its own comma.
    cleaned = cleaned.lstrip(",;: ").strip()
    if not cleaned:
        return ""

    cleaned = _CASED_START.sub(lambda match: match.group().upper(), cleaned)
    if cleaned[-1] not in _TERMINAL:
        cleaned += "."
    return cleaned
