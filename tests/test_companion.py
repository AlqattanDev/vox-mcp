"""The companion tier: scope policy, backend bridge, and the handoff loop."""

import asyncio
import json
from pathlib import Path

import pytest

from voxmcp.companion import CompanionReply, _extract, _resolve_grokctl, ask_companion
from voxmcp.intents import companion_may_answer


ANSWERABLE = [
    "hey",
    "how is it going",
    "yeah",
    "still there",
    "ok cool",
    "thanks",
    "haha nice",
    "yeah alright",      # short enough not to be a request
    "man im tired",
    # Real utterances from the live verification run.
    "so man how are you doing today hows things",
    "hello hi how are you",
    "im just waiting man",
]

MUST_ESCALATE = [
    "why does engine.py crash",
    "can you fix the tests",
    "what did you change",
    "show me the diff",
    "how do i run pytest",
    "the barge_in_control is none",
    "what is wrong with my code",
    "run git status",
    "check src/voxmcp/audio.py",
    "did the deploy work",
    # Long enough that it is far more likely a real request than a pleasantry.
    "so anyway i was thinking about the whole architecture and whether we should change it",
    # Ali actually said this and a permissive fallback answered it.
    "okay so tell me exactly like what all the changes that",
    "are you done yet",
    "how long is this going to take",
    "is it working now",
    # A greeting does not launder a work question riding along with it.
    "hello hi how are you so im asking you about the code of vox what changes did we do",
]


@pytest.mark.parametrize("utterance", ANSWERABLE)
def test_companion_answers_small_talk(utterance: str) -> None:
    assert companion_may_answer(utterance) is True


@pytest.mark.parametrize("utterance", MUST_ESCALATE)
def test_companion_escalates_anything_about_the_work(utterance: str) -> None:
    # Under-escalating means a model that has never seen the project answers a
    # question about it confidently. That is the failure worth over-paying for.
    assert companion_may_answer(utterance) is False


def test_silence_is_never_answerable() -> None:
    assert companion_may_answer("") is False
    assert companion_may_answer("   ") is False


def test_verb_result_extraction_keeps_named_failure_reasons() -> None:
    # llm-spending-limit is the reason an operator acts on; flattening it into
    # a generic failure would hide what actually needs fixing.
    ok, text, reason = _extract(
        {"ok": True, "reason": "ask xai 900ms",
         "evidence": {"promptStudio": {"text": "  all good  "}}}
    )
    assert (ok, text) == (True, "all good")

    ok, _text, reason = _extract(
        {"ok": False, "reason": "llm-spending-limit", "evidence": {"errorMessage": "no credits"}}
    )
    assert ok is False
    assert reason == "llm-spending-limit"

    # ok=true with nothing in it is a failure, not an empty answer to speak.
    ok, _text, reason = _extract({"ok": True, "evidence": {"promptStudio": {"text": ""}}})
    assert (ok, reason) == (False, "companion_empty_reply")


@pytest.mark.asyncio
async def test_a_missing_backend_reports_instead_of_raising(monkeypatch) -> None:
    # The companion is a courtesy. A missing grokctl must degrade to "no
    # companion", never take the voice turn down with it.
    monkeypatch.setenv("VOX_COMPANION_COMMAND", "")
    monkeypatch.setattr("voxmcp.companion.shutil.which", lambda _name: None)
    monkeypatch.setattr("voxmcp.companion.Path.is_file", lambda _self: False)

    reply = await ask_companion("hey")

    assert reply.ok is False
    assert reply.reason == "companion_backend_missing"


@pytest.mark.asyncio
async def test_backend_output_that_is_not_json_is_reported_cleanly(monkeypatch) -> None:
    monkeypatch.setenv("VOX_COMPANION_COMMAND", "/bin/echo not-json-at-all")
    reply = await ask_companion("hey")
    assert reply.ok is False
    assert reply.reason == "companion_bad_output"


@pytest.mark.asyncio
async def test_a_real_backend_reply_is_parsed(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "ok": True,
        "reason": "ask xai 800ms",
        "evidence": {"promptStudio": {"text": "Still here with you."}},
    }
    script = tmp_path / "fake-grokctl"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(payload)}\nEOF\n")
    script.chmod(0o755)
    monkeypatch.setenv("VOX_COMPANION_COMMAND", str(script))

    reply = await ask_companion("hey")

    assert reply.ok is True
    assert reply.text == "Still here with you."
    assert reply.backend == "grokctl"


def test_an_explicit_command_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("VOX_COMPANION_COMMAND", "/usr/bin/env grokctl")
    assert _resolve_grokctl() == ("/usr/bin/env", "grokctl")
