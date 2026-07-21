# The Vox listening contract

Vox treats microphone access as a short-lived capability, not as a side effect
of “voice mode.” A session can remain active for convenience while the
microphone is physically closed.

## When Vox listens

| Situation | Microphone |
|---|---|
| Vox runtime starts | Closed |
| A voice session starts | Closed; state becomes `IDLE` |
| `speak` or `converse(..., wait_for_response=false)` | Closed |
| `converse(..., wait_for_response=true)` | Opens only after playback has fully drained |
| `listen` | Opens immediately for one bounded turn |
| A `listen` turn is used as push-to-talk | Open until manual end, then closed |
| The user says a short standalone “wait” phrase | Closed during the wait, then reopened for a fresh bounded turn |
| No speech arrives within the onset timeout | Closed; session remains `IDLE` |
| Speech ends | Closed after adaptive trailing silence |
| Pause, mute, stop, Escape/cancel, sleep, screen lock, route loss, or request cancellation | Closed immediately |

There is no ambient listening by default. Starting the runtime, connecting an
MCP client, or leaving a voice session active does not open the microphone.

Default turn bounds are deliberately generous for this user’s speaking style:

- 300 ms pre-roll so first syllables survive detection.
- 15 seconds to begin speaking.
- 1.2 seconds of trailing silence to end a normal utterance.
- Up to 300 seconds for a long dictated turn.

All four are configurable. Unlike VoiceMode, the speech-onset timeout and
trailing-silence timeout are separate.

## Session and turn controls

| User intent | Effect |
|---|---|
| “Stop voice mode” / “switch to text” | Ends the session, stops audio, closes mic |
| “Pause voice mode” / “mute” | Keeps the session but blocks new listening and closes the mic |
| “Resume voice mode” | Returns a paused session to `IDLE`; it still does not listen until asked |
| Panel Mode cycle | Talk (both) → Narrate (agent only) → Dictate (you only) |
| Panel Stop / Start | Ends session or opens shared session; never exclusive ownership |
| Panel **I'm done talking** | Closes the current recording and transcribes it; keeps the spoken message |
| Panel Cancel this turn | Cancels only the active speak/listen; session stays up |
| “Repeat that” | Replays cached audio; Kokoro is not called again |
| “Wait” / “give me a second” | Closes the mic, waits, then listens again |
| Escape or `voice_control(cancel)` | Cancels only the current turn; voice mode remains active |
| Manual end-of-turn | Closes the current recording immediately and transcribes what was captured |

Spoken controls are recognized only as short standalone utterances. A long
dictation containing the words “stop voice mode” is treated as content, not a
control command.

## Shared session (no exclusive owner)

Nobody owns the voice session. Grok, Fable, Claude, Codex — and any other MCP
host — join the **same** session. Each agent keeps its own Kokoro voice via
`?agent=` / `agents.json`. Hardware is serialized only by the **FIFO
OperationGate**: if someone is speaking or listening, others wait, then go.

- `BusyError` means the queue wait timed out or the session is paused — not
  “another agent owns voice.”
- `handoff` / `takeover` are legacy: takeover cancels the active turn and drains
  the queue; handoff is a shared no-op.
- Global `stop` / panel Stop / sleep pause close the mic for everyone (privacy).

## IO modes

| Mode | Agent speaks | Mic opens | Use |
|---|---|---|---|
| `talk` (default) | yes | yes after TTS | Normal conversation |
| `narrate` | yes | no | Agent talks; user types |
| `dictate` | no | yes | User talks; agent does not TTS |

Set via panel cycle, `voice_session(set_mode|cycle_mode)`, or `POST /control`.

## Undelivered transcript recovery

When Whisper finishes, Vox writes `~/.vox/state/last_heard.json` before the
tool returns. If the MCP host cancels after STT, the runtime returns the
transcript with `delivered_via: cancel_recovery`. Agents can also call
`voice_session(claim_undelivered)` or `transcribe(latest=true)`.

## Failure behavior

- A Kokoro failure triggers one bounded local restart/retry, then falls back
  to macOS `say`. It does not end the voice session.
- A Whisper HTTP failure triggers one bounded local restart/retry, then uses
  the installed `whisper-cli` and local model.
- Cancellation signals a thread-safe stop event and waits for the recorder to
  confirm closure before releasing the audio lock.
- A host update can restart Claude Code or Codex without killing Vox because
  both connect to the persistent HTTP runtime.
- Vox never silently tries a cloud endpoint.
