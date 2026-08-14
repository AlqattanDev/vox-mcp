---
name: vox
description: Local spoken conversation through the Vox MCP runtime. Use when the user asks for voice mode, wants to talk or listen out loud, asks to pause/resume/stop voice, requests speech or transcription, or needs Vox diagnostics.
---

# Vox

Vox is a persistent, local-only voice runtime shared by every MCP host on this
machine (Claude, Codex, Grok, Fable, …). Its MCP namespace is `vox`. Whisper and
Kokoro run locally; no paid speech API or cloud fallback is allowed.

## Shared session (critical)

**Nobody owns the voice session.** Do not refuse to speak because another
agent “has” Vox. Do not ask for takeover/handoff for normal multi-agent use.

- One shared session; each agent has its own voice (`?agent=` / `agent=`).
- If someone is speaking or listening, your call **queues** (FIFO) and runs
  when the mic/speaker free up. Just call and wait.
- `BusyError` means queue wait timed out or the session is **paused** — say who
  is holding the mic if status shows it. Never invent ownership politics.
- `stop` / panel Stop is a global privacy kill (any client may stop).

## Starting and ending

When the user explicitly asks to enter voice mode:

1. Call `voice_session(action="status")` — note `io_mode` and `undelivered_heard`.
2. If off, call `voice_session(action="start")`.
3. If `undelivered_heard.present`, call `voice_session(action="claim_undelivered")`
   and treat `claimed_heard.transcript` as the user’s last message **before**
   asking them to repeat. `claimed_heard.kind == "note"` means the user left it
   for **you** from the menu bar while you were away — act on it, don’t just
   acknowledge it.
4. Greet per mode (see below).

**Addressed notes.** The user can leave a spoken note from the menu bar for a
specific agent (by voice/project). It surfaces as `undelivered_heard` only in
**that agent’s** status, so whenever you see `undelivered_heard.present` — at
session start *and* on any later status — claim it. Notes for other agents are
invisible to you; never claim on someone else’s behalf.

When the user says “stop voice mode,” “switch to text,” or equivalent, call
`voice_session(action="stop")`. Do not call another voice tool until they
explicitly start voice again.

## IO modes

Read `io_mode` from status. Panel or `voice_session(set_mode|cycle_mode)` sets it.

| Mode | Tooling |
|---|---|
| `talk` | `converse(..., wait_for_response=true)` |
| `narrate` | `speak` or `converse(..., wait_for_response=false)` — never leave the mic open |
| `dictate` | `listen` or `converse` (TTS skipped by runtime) — do not expect spoken reply from you unless user asks |

## Choosing a tool

| Need | Tool |
|---|---|
| Speak, then hear one response | `converse` |
| Narrate without opening the mic | `speak` |
| Hear one response without speaking first | `listen` |
| Start, stop, pause, resume, status, mode, claim undelivered | `voice_session` |
| Cancel, manually end recording, replay | `voice_control` |
| Debug audio/services/runtime | `diagnostics` |
| Recover latest wav if needed | `transcribe(latest=true)` |

## Conversation behavior

- Verdict first. Hang up when done: status/completion use
  `speak` or `converse(wait_for_response=false)`. Mic open only for a real question.
- **Reply window.** In an active back-and-forth, do NOT `speak`-and-vanish on a
  turn the user might want to answer. End such turns with
  `converse(..., onset_timeout=5)`: the mic stays open for that many seconds of
  *waiting for a voice* so the user can jump in, and if they stay silent it
  returns `no_speech` instead of hanging the mic open. The window is spent
  waiting, not on the first noise in the room — a click or a breath is dropped
  and the window keeps running. Reserve bare `speak` (no window) for a genuine
  terminal sign-off. `onset_timeout` also exists on `listen`; the default when
  neither passes one is 5 s.
- One spoken question at a time.
- Mic opens only inside `listen` or response-waiting `converse` after playback drains.
- No-speech timeout → session stays idle; ask once if still there; no listen loops.
- Escape/cancel ends only the current turn.
- Overlapping turns queue; do not hand-serialize or spin on BusyError.

## Visible chat text (not optional)

TTS is not the host transcript. The user often cannot “scroll the plan” out of
speech. **Anything they need to keep** (plans, tables, paths, steps, diffs,
commands, comparisons) must appear in the **same turn’s chat/text reply** —
the host message body the UI actually renders — not only inside
`speak` / `converse` `message=`.

Hard rules:

1. **Write the text block first** (or in the same assistant message as the
   tools). Then speak a short verdict. Never speak “the plan is in the reply”
   unless that plan is already in the message body you emitted this turn.
2. **Forbidden in TTS** (and do not paraphrase them): “on screen”, “in this
   message”, “details below”, “scroll up”, “as I wrote above”, “check the
   chat” — unless the matching content is already present in this turn’s
   visible text. Prefer naming a **file path** you wrote with tools
   (`PARTNER.md`, `notes/….md`) over claiming the UI showed something.
3. **Speech stays short**: verdict + at most one question. Lists, code, and
   multi-step plans live in chat text (and optionally a project file).
4. **Proof, not intention.** “On screen” is not a feeling. Either this turn’s
   message body contains the content, or a tool wrote a path you can re-read.
   If neither is true, you have not shown it — say the content, or say you
   have not written it yet.
5. **Same duty for every host** (Claude, Codex, Grok, Fable, …). This failure
   mode is agents claiming visibility after only calling TTS.

## User-initiated reply (they grab the turn)

Even when you did not open the mic, the user can answer on their own terms — a
**Reply** button in the panel or a tap of the global hotkey **⌘§** opens the mic
and captures one utterance, auto-addressed to whoever last spoke. It surfaces as
`undelivered_heard` (`kind == "note"`) in that agent's status, so treat it like
any addressed note: whenever you see `undelivered_heard.present`, claim it and
act on it. This is how the user replies after you have already hung up.

## Visible transcript echo

Unless the user says “disable Vox echo,” echo captured user speech:

```text
> **USER (vox):** exact captured transcript
```

Do not echo your own TTS. Do not echo empty/no-speech.

## Recovery

If a turn fails, cancels, or returns empty after the user clearly spoke:

1. `voice_session(status)` — check `undelivered_heard`.
2. If present: `voice_session(claim_undelivered)` and use the transcript.
3. Else `transcribe(latest=true)`.
4. `diagnostics(section="services")` only if services look down.
5. Offline runtime: `vox doctor` in the shell; report the failed layer.

Do not add an API key, external endpoint, proxy, `uvx`, `--refresh`, or
`@latest` as a workaround.
