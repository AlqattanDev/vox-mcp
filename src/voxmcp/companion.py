"""The fast conversational tier that keeps the user company while an agent works.

Vox itself opens no sockets for this.  It shells out to ``grokctl ask``, which
already owns the xAI OAuth credential and the retry discipline that goes with
it, and which makes the outbound call on Vox's behalf.  That keeps
``VoxConfig.local_only`` a literal, unqualified statement about this process
while still being honest in ``diagnostics(section="privacy")`` that a companion
turn reaches the network through a sibling.

The companion never answers questions about the user's work.  Scope is decided
here, in Python, by ``companion_may_answer`` — the backend is a dumb pipe and
is never asked to police itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


DEFAULT_TIMEOUT_S = 30.0

# A pseudo-agent label so the companion gets its own hash-assigned Kokoro voice
# from agents.json. Hearing the same voice would make it sound like the real
# agent had suddenly gone vague about its own work.
COMPANION_AGENT = "companion"

COMPANION_SYSTEM = (
    "You are a warm, brief companion keeping someone company while their coding "
    "agent works on their project. You are NOT the coding agent. You know nothing "
    "about their code and you never guess about it. Reply in ONE short spoken "
    "sentence — this is read aloud, so no lists, no markdown, no code. If they "
    "ask anything about their project, their code, or what the agent is doing, "
    "say you will pass it along rather than answering."
)


@dataclass(frozen=True, slots=True)
class CompanionReply:
    """One completed round trip to the companion backend."""

    ok: bool
    text: str
    reason: str
    elapsed_ms: int
    backend: str = "grokctl"


def _resolve_grokctl() -> tuple[str, ...] | None:
    """Find the grokctl entry point, preferring an explicit override."""

    override = os.environ.get("VOX_COMPANION_COMMAND", "").strip()
    if override:
        return tuple(override.split())
    binary = shutil.which("grokctl") or _first_existing(
        Path.home() / ".bun" / "bin" / "grokctl",
        Path("/opt/homebrew/bin/grokctl"),
    )
    if binary:
        return (str(binary),)
    # The repo checkout is the normal case on this machine; grokctl is a bun
    # project and is not always on PATH as a shim. launchd starts the runtime
    # with a minimal PATH that contains neither bun nor anything installed by
    # a version manager, so the well-known locations are checked directly —
    # relying on which() alone meant the companion was simply "missing" on the
    # installed runtime while working perfectly from a shell.
    entry = Path.home() / "grokctl" / "src" / "cli.ts"
    bun = shutil.which("bun") or _first_existing(
        Path.home() / ".bun" / "bin" / "bun",
        Path("/opt/homebrew/bin/bun"),
        Path("/usr/local/bin/bun"),
    )
    if bun and entry.is_file():
        return (str(bun), "run", str(entry))
    return None


def _first_existing(*candidates: Path) -> Path | None:
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _extract(payload: dict) -> tuple[bool, str, str]:
    """Pull answer, or the named failure reason, out of a VerbResult."""

    reason = str(payload.get("reason") or "")
    evidence = payload.get("evidence") or {}
    if payload.get("ok"):
        studio = evidence.get("promptStudio") or {}
        text = str(studio.get("text") or "").strip()
        if text:
            return True, text, reason
        return False, "", "companion_empty_reply"
    # grokctl's named reasons are the useful part: llm-spending-limit and
    # llm-not-authorized tell the operator exactly what to fix.
    detail = str(evidence.get("errorMessage") or "")
    return False, "", reason or detail or "companion_failed"


async def ask_companion(
    prompt: str,
    *,
    system: str = COMPANION_SYSTEM,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> CompanionReply:
    """Ask the companion backend one question. Never raises."""

    command = _resolve_grokctl()
    if command is None:
        return CompanionReply(False, "", "companion_backend_missing", 0)

    loop = asyncio.get_running_loop()
    started = loop.time()

    def elapsed() -> int:
        return int((loop.time() - started) * 1000)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            "ask",
            prompt,
            "--system",
            system,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return CompanionReply(False, "", f"companion_spawn_failed: {exc}", elapsed())

    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return CompanionReply(False, "", "companion_timeout", elapsed())

    try:
        payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return CompanionReply(False, "", "companion_bad_output", elapsed())
    if not isinstance(payload, dict):
        return CompanionReply(False, "", "companion_bad_output", elapsed())

    ok, text, reason = _extract(payload)
    return CompanionReply(ok, text, reason, elapsed())
