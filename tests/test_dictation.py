from __future__ import annotations

import pytest

from voxmcp.dictation import clean_dictation


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("hello there", "Hello there."),
        ("Already fine.", "Already fine."),
        ("what about this?", "What about this?"),
        ("  padded   out  ", "Padded out."),
    ],
)
def test_it_finishes_the_sentence_whisper_started(raw: str, expected: str) -> None:
    assert clean_dictation(raw) == expected


def test_hesitation_is_dropped_but_words_are_not() -> None:
    assert clean_dictation("um, I think uh we should ship it") == "I think we should ship it."


def test_a_filler_inside_a_word_survives() -> None:
    """"um" is a noise; "umbrella" is a word. Only whole words are fillers."""

    assert clean_dictation("bring the umbrella and the hummus") == (
        "Bring the umbrella and the hummus."
    )


def test_whisper_annotations_never_get_typed() -> None:
    assert clean_dictation("[BLANK_AUDIO]") == ""
    assert clean_dictation("  (wind)  ") == ""
    assert clean_dictation("[MUSIC] ship it [door closes]") == "Ship it."


@pytest.mark.parametrize("raw", [None, "", "   ", 42])
def test_nothing_said_means_nothing_typed(raw) -> None:
    assert clean_dictation(raw) == ""


def test_arabic_round_trips_without_being_mangled() -> None:
    """Arabic has no capital form, and the text must survive byte for byte."""

    assert clean_dictation("خلنا نشوف الكود") == "خلنا نشوف الكود."


def test_arabic_hesitation_is_dropped_too() -> None:
    assert clean_dictation("اه خلنا نشوف") == "خلنا نشوف."


def test_off_mode_keeps_the_transcript_verbatim() -> None:
    assert clean_dictation("um, as spoken", mode="off") == "um, as spoken"
    # The non-speech check still applies: an annotation is never words.
    assert clean_dictation("[BLANK_AUDIO]", mode="off") == ""


def test_a_sentence_left_bare_by_filler_removal_is_repaired() -> None:
    assert clean_dictation("um, ship it") == "Ship it."
    assert clean_dictation("uh") == ""
