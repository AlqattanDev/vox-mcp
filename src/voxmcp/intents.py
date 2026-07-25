"""Conservative whole-utterance spoken control intent recognition."""

from __future__ import annotations

import re
import unicodedata
from types import MappingProxyType

from .models import IntentMatch, SpokenIntent


# These are intentionally small exact-match sets.  A control command must be
# the entire utterance; prose that happens to contain "stop" or "repeat" is not
# commandeered by the voice layer.
_PHRASES = MappingProxyType(
    {
        SpokenIntent.STOP: frozenset(
            {
                "stop",
                "stop voice mode",
                "please stop voice mode",
                "exit voice mode",
                "end voice mode",
                "turn voice mode off",
                "close voice mode",
                "switch to text",
                "switch back to text",
                "stop the survey",
            }
        ),
        SpokenIntent.PAUSE: frozenset(
            {
                "pause",
                "pause voice mode",
                "please pause",
                "hold on",
                "hang on",
                "mute",
                "mute voice mode",
            }
        ),
        SpokenIntent.RESUME: frozenset(
            {
                "resume",
                "resume voice mode",
                "please resume",
                "continue listening",
                "im back",
            }
        ),
        SpokenIntent.REPEAT: frozenset(
            {
                "repeat",
                "repeat that",
                "please repeat",
                "say that again",
                "can you repeat that",
            }
        ),
        SpokenIntent.WAIT: frozenset(
            {
                "wait",
                "please wait",
                "wait a moment",
                "give me a second",
                "give me a minute",
            }
        ),
        SpokenIntent.END_TURN: frozenset(
            {
                "over",
                "end turn",
                "end my turn",
                "done speaking",
                "im done",
                "thats all",
                "send it",
            }
        ),
    }
)

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

# whisper.cpp annotates silence and ambient sound rather than returning nothing:
# [BLANK_AUDIO], [MUSIC], [wind], (door closes). A transcript made only of those
# is a label about the audio, never words a person said.
_NON_SPEECH_MARKERS_ONLY = re.compile(r"^(?:\s*[\[(][^\])]*[\])]\s*)+$")


def is_non_speech_transcript(text: str | None) -> bool:
    """True when a transcript carries no user words at all.

    Passing a marker on to an agent as though the user had spoken it is worse
    than reporting silence: it made the companion escalate ``out_of_scope`` on a
    quiet room — the exact moment it exists to cover — and it would let a stray
    ``[BLANK_AUDIO]`` answer for the user in any ordinary listen.
    """

    if not isinstance(text, str):
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return bool(_NON_SPEECH_MARKERS_ONLY.match(stripped))
_WAIT_DURATION = re.compile(
    r"^(?:please\s+)?wait(?:\s+for)?\s+"
    r"(?P<amount>\d{1,3}|one|two|three|four|five|ten)\s+"
    r"(?P<unit>seconds?|minutes?)$"
)
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "ten": 10,
}


def normalize_utterance(text: str) -> str:
    """Normalize punctuation and whitespace without changing word content."""

    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    # Apostrophes are removed so "I'm" and "that's" match stable phrases.
    normalized = normalized.replace("'", "").replace("’", "")
    normalized = _PUNCTUATION.sub(" ", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()


def classify_spoken_intent(text: str) -> IntentMatch:
    """Classify an utterance only when it exactly matches a control command."""

    normalized = normalize_utterance(text)
    if not normalized or len(normalized) > 80 or len(normalized.split()) > 8:
        return IntentMatch(SpokenIntent.NONE, normalized)

    for intent, phrases in _PHRASES.items():
        if normalized in phrases:
            return IntentMatch(intent, normalized, matched_phrase=normalized)

    wait_match = _WAIT_DURATION.fullmatch(normalized)
    if wait_match:
        raw_amount = wait_match.group("amount")
        amount = _NUMBER_WORDS.get(raw_amount, int(raw_amount) if raw_amount.isdigit() else 0)
        unit = wait_match.group("unit")
        seconds = float(amount * (60 if unit.startswith("minute") else 1))
        # A spoken wait is bounded.  Longer absences should use an explicit
        # session pause so a transcription mistake cannot wedge the channel.
        if 1 <= seconds <= 30 * 60:
            return IntentMatch(
                SpokenIntent.WAIT,
                normalized,
                matched_phrase=normalized,
                duration_seconds=seconds,
            )

    return IntentMatch(SpokenIntent.NONE, normalized)


# A short alias is convenient for adapters and remains unambiguous.
parse_spoken_intent = classify_spoken_intent


# Everything the companion is allowed to answer on its own.  Anything outside
# this is escalated to the real agent — the list is a whitelist and it stays
# short on purpose.  A companion that starts explaining your codebase is the
# failure this whole design exists to prevent.
_COMPANION_SMALL_TALK = frozenset(
    {
        "hi", "hey", "hello", "yo", "sup", "morning", "good morning",
        "good evening", "thanks", "thank you", "cheers", "nice", "cool",
        "ok", "okay", "right", "sure", "yeah", "yep", "yes", "no", "nope",
        "mhm", "uh huh", "go on", "keep going", "carry on", "still there",
        "you there", "how are you", "hows it going", "how is it going",
        "whats up", "what's up", "im back", "im here", "just waiting",
        "nothing", "never mind", "nevermind", "sorry", "my bad", "haha",
        "lol", "fair", "fair enough", "makes sense", "true", "bye",
    }
)

# Shapes that mean "this is about the work" no matter how they are phrased.
_CODE_SHAPED = re.compile(
    r"""
      (?: \b[\w-]+\.(?:py|ts|tsx|js|jsx|swift|rs|go|java|rb|sh|json|toml|yaml|yml|md)\b )
    | (?: \b\w+ \s* \( \s* \) )                 # a function call
    | (?: \b\w+ (?: _\w+ )+ \b )                # snake_case identifier
    | (?: \b(?:src|tests|docs)/ )               # a repo path
    | (?: \b(?:git|npm|pytest|bun|uv|cargo|launchctl|ssh|aws|terraform)\b )
    """,
    re.VERBOSE,
)

_WORK_TOPIC = re.compile(
    r"""\b(?:
        code | codebase | repo | repository | commit | branch | merge | diff
      | bug | crash | stack \s* trace | error | exception | traceback
      | test | tests | build | compile | deploy | deployment | rollback
      | function | class | method | variable | parameter | api | endpoint
      | database | schema | migration | config | environment
      | why \s+ (?:does|did|is|isn't|isnt|are|aren't|arent)
      | what'?s? \s+ wrong
      | how \s+ do \s+ i
      | can \s+ you \s+ (?:fix|change|add|write|implement|refactor|check|look)
      | show \s+ me
      | what \s+ (?:did|are) \s+ you \s+ (?:do|doing|change|changing)
      | change | changes | changed | fix | fixed | broke | broken
      # "is it working now", but not "while this guy is working" — a bare
      # `working` escalated ordinary conversation about the person working.
      | (?:it|that|this|everything|anything) \s+ working
      | working \s+ (?:now|yet|again)
      | tell \s+ me | explain | walk \s+ me \s+ through | status | progress
      | done \s+ yet | finished \s+ yet | how \s+ long
    )\b""",
    re.VERBOSE,
)


# Positive markers of chit-chat. Presence of one of these is what earns an
# answer; absence sends the utterance back to the real agent.
_SMALL_TALK_SIGNAL = re.compile(
    r"""\b(?:
        hi | hey | hello | yo | salam | morning | evening | night
      | how \s+ (?:are|r) \s+ (?:you|u) | how(?:s|\s+is|\s+are)? \s+ (?:it|things|life|you)
      | whats \s+ up | sup | you \s+ (?:there|still \s+ there)
      | thanks | thank \s+ you | cheers | appreciate
      | tired | bored | hungry | sleepy | exhausted | stressed
      | nice | cool | lol | haha | funny | true | fair
      | bye | goodnight | later | see \s+ you
      | im \s+ (?:back|here|good|fine|ok|okay|tired|bored)
      | just \s+ (?:waiting|chilling|here)
      | nothing \s+ much | never \s* mind | my \s+ bad | sorry
    )\b""",
    re.VERBOSE,
)


# Asking the companion to stop, phrased as a person actually phrases it. Kept
# separate from _PHRASES because the global parser requires the command to be
# the whole utterance — correct when prose must never be commandeered, wrong
# here, where small talk is the only thing in scope and there is no sentence in
# which "stop" deserves an answer rather than obedience.
# "enough" is pinned to explicit shapes so "fair enough" and "good enough" stay
# ordinary conversation, and there is no bare "quit" because Ali was discussing
# quitting smoking at the time.
_COMPANION_STOP_REQUEST = re.compile(
    r"""(?:
        \b stop \b
      | \b shut \s+ up \b
      | \b be \s+ quiet \b
      | \b shush \b
      | \b thats \s+ enough \b | \b that \s+ is \s+ enough \b
      | \b enough \s+ now \b | \b enough \s+ of \s+ (?:that|this|it) \b
      | \b we \s* re \s+ done \b | \b we \s+ are \s+ done \b
      | \b go \s+ away \b
      | \b leave \s+ me \s+ alone \b
    )""",
    re.VERBOSE,
)


def companion_should_stop(text: str) -> bool:
    """True when the user is asking the companion to stop talking.

    "I think it's time you stop" drew a cheerful "I'll pass that along" and the
    loop carried on; the next thing said aloud was "I thought stop will stop
    you". Obeying costs a round trip back to the real agent, which is exactly
    what the user asked for.
    """

    normalized = normalize_utterance(text)
    if not normalized:
        return False
    return bool(_COMPANION_STOP_REQUEST.search(normalized))


def companion_may_answer(text: str) -> bool:
    """True only when the companion can safely answer without the real agent.

    Deliberately biased toward escalation: this is a text heuristic, not a
    classifier, so every ambiguous case goes to the agent that actually knows
    the code.  Over-escalating costs a round trip; under-escalating means a
    model with no knowledge of the project answers a question about it
    confidently, which is the failure mode worth paying to avoid.
    """

    normalized = normalize_utterance(text)
    if not normalized:
        return False
    # Code shapes live in exactly the punctuation normalize_utterance strips —
    # "audio.py" and "src/" survive only in the raw text — so that check runs
    # before normalization and the topic check runs after it.
    if isinstance(text, str) and _CODE_SHAPED.search(text.casefold()):
        return False
    if normalized in _COMPANION_SMALL_TALK:
        return True
    if _CODE_SHAPED.search(normalized) or _WORK_TOPIC.search(normalized):
        return False
    # Past the exact phrases, an utterance is answered only when it carries a
    # positive marker of chit-chat — not merely because nothing flagged it.
    # Matching signals rather than whole utterances is what lets "so man, how
    # are you doing today?" through while "tell me exactly what all the
    # changes" still goes to the agent that knows the code.
    words = normalized.split()
    # The signal check comes before the length cap on purpose. Ali rambled for
    # fifty words about not knowing what to chat about, ending in "how are you
    # and are you good" — unmistakable small talk that the cap escalated before
    # the signal was ever consulted. Length is a weak proxy for "this is a
    # request"; an explicit chit-chat marker that survived every veto is
    # stronger evidence than word count.
    if _SMALL_TALK_SIGNAL.search(normalized):
        return True
    if len(words) > 16:
        return False
    # Nothing above matched: no filename, no identifier, no work topic and no
    # request shape. Demanding a positive chit-chat keyword *as well* made the
    # companion unusable rather than safe — the first natural thing said to it,
    # "so what do you want to talk to me about", carries no keyword from any
    # list and was escalated as if it were a question about the code. The vetoes
    # are what protect the project; this fallback only ever blocked conversation
    # the vetoes had already cleared.
    return True
