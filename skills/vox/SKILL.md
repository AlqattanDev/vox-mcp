---
name: vox
description: Local spoken conversation through the Vox MCP runtime. Use when the user asks for voice mode, wants to talk or listen out loud, asks to pause/resume/stop voice, requests speech or transcription, or needs Vox diagnostics.
---

# Vox

Vox is a persistent, local-only voice runtime shared by Claude Code and Codex.
Its MCP namespace is `vox`. Whisper and Kokoro run on this machine; no paid
speech API or cloud fallback is allowed.

## Starting and ending

When the user explicitly asks to enter voice mode:

1. Call `voice_session(action="status")`.
2. If off, call `voice_session(action="start")`.
3. Greet with `converse(message=..., wait_for_response=true)`.

When the user says “stop voice mode,” “switch to text,” or equivalent, Vox
returns `control.action="stop"` and ends the session. Do not call another voice
tool until the user explicitly starts voice again.

`voice_session(action="pause")` keeps the session but guarantees the
microphone is closed. `resume` returns to idle; it does not begin listening.

## Choosing a tool

| Need | Tool |
|---|---|
| Speak, then hear one response | `converse` |
| Narrate without opening the mic | `speak` |
| Hear one response without speaking first | `listen` |
| Start, stop, pause, resume, status, handoff | `voice_session` |
| Cancel, manually end recording, replay | `voice_control` |
| Several indexed questions with partial results | `voice_survey` |
| Debug audio/services/runtime | `diagnostics` |
| Recover or transcribe an audio file | `transcribe` |

Use default timing unless the user asks otherwise. Vox already has a
15-second speech-onset timeout, 1.2-second trailing-silence endpoint, 300 ms
pre-roll, and a five-minute hard utterance cap.

## Conversation behavior

- Ask one spoken question at a time.
- Keep speech natural and somewhat shorter than screen prose. Put code, paths,
  tables, and long lists on screen instead of reading them verbatim.
- Use `speak` for short progress narration and do the independent work in
  parallel where the host permits it.
- The microphone opens only inside `listen` or response-waiting `converse`,
  after playback has drained. Never imply that Vox is “always listening.”
- A no-speech timeout leaves voice mode active and idle. Ask once whether the
  user is still there; do not create an infinite listen loop.
- Escape/cancel ends only the current turn. It does not end voice mode.
- Standalone “repeat that” replays cached audio without regenerating it.
- Standalone “wait” closes the mic, waits, then opens a fresh bounded listen.
- Overlapping audio turns queue automatically in arrival order, so do not
  serialise calls by hand or retry on your own; just make the call and wait.
- A `BusyError` therefore means one of two things. If it names another owner,
  the audio lease belongs to a different MCP host: offer an explicit handoff.
  If it says the wait timed out, someone held the microphone too long — say so
  and let the user decide. Do not spin or retry until the MCP timeout.

## Visible transcript echo

MCP calls can be collapsed by some hosts. Unless the user says “disable Vox
echo,” keep the transcript visible:

```text
> **ASSISTANT (vox):** exact message sent to converse/speak
[tool call]
> **USER (vox):** exact captured transcript
```

Write the assistant echo before the tool call. Write the user echo in the next
visible response after capture. Do not echo empty/no-speech results, and do not
duplicate text the host already renders visibly.

## Recovery

If a normal turn fails:

1. Call `diagnostics(section="services")`.
2. Vox automatically performs one bounded service restart/retry. Do not start
   package installers or change models during a conversation.
3. If STT failed after audio was captured, call `transcribe(latest=true)`.
4. If the runtime is offline, use `vox doctor` in the shell and report the
   exact failed layer.

Do not add an API key, external endpoint, proxy, `uvx`, `--refresh`, or
`@latest` as a workaround.

