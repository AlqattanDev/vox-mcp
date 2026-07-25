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
| `converse(...)` with barge-in enabled | Opens *during* playback, gated against Vox's own voice, so talking interrupts |
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
- 0.6 to 1.6 seconds of trailing silence to end an utterance, scaled to how much
  was actually said (see below).
- Up to 75 seconds for a long dictated turn.

All four are configurable. Unlike VoiceMode, the speech-onset timeout and
trailing-silence timeout are separate.

### Calibrate the speech floor first

Everything below assumes Vox can tell your silence from your speech, and on a
laptop microphone that is not automatic. WebRTC VAD votes "speech" on ordinary
room tone, so if your room sits above `minimum_speech_dbfs` the runtime hears
continuous talking: trailing silence never accumulates and a one-word answer
takes many seconds to end.

```bash
vox calibrate
```

It measures your quiet room, then your voice, and prints the floor that clears
the room's *peaks* plus the command to apply it. Note the applied value only
lasts the login session — re-run it after a reboot.

### Trailing silence scales with the utterance

A one-word answer and a rambling paragraph should not wait the same amount of
time to be considered finished. Vox measures *speech* duration — time in frames
classified as speech, which pauses while you pause and resumes when you start
again — and interpolates the trailing-silence requirement from it:

| Speech so far | Silence needed to close |
|---|---|
| Up to 1.5s (“keep”, “remove”, “yeah do it”) | 0.6s |
| 1.5s → 3.0s | Interpolated, 0.6s → 1.6s |
| Over 3.0s | 1.6s |

A long answer keeps the full patience it has always had, so nothing gets cut
off mid-thought; a short one stops making you wait for it. Because the clock
keys on speech and not wall time, pausing mid-sentence to think does not
demote a long answer to the fast path. Tune with
`VOX_SHORT_TRAILING_SILENCE_SECONDS`, `VOX_SHORT_UTTERANCE_SPEECH_SECONDS`, and
`VOX_LONG_UTTERANCE_SPEECH_SECONDS`.

## The voice turn contract

The microphone and the speaker are fast. Measured over real exchanges, Vox owns
2–4 seconds of a turn — time to first audio, Whisper, trailing silence — and
the agent owns 25–40. Almost all of that is the agent doing desk work (reading
files, running searches, composing a long answer with tables) inside what is
supposed to be a conversation.

So the contract is on the agent, not the runtime. It ships in the MCP server
instructions, the `voice_mode` prompt, and the `converse` tool description:

- **One or two sentences.** Answer first, then stop. Length is latency.
- **No tool calls between conversation turns.** Finish investigating before
  opening the mic, not after.
- **Nothing read aloud that belongs on screen** — no tables, code, file paths,
  or lists. Say the conclusion.
- **Announce work before doing it.** `speak("reading the audio path now")`
  beats going quiet; unexplained silence reads as a crash.
- **Low reasoning effort for conversational turns.** Save the deep thinking for
  the work, not for deciding what to say back.
- **`wait_for_response=false` for status updates and sign-offs.** Never make the
  user confirm that you finished.

## Talking over Vox (barge-in)

Off by default. `VOX_BARGE_IN_ENABLED=1` opens the microphone alongside
playback so interrupting is something you *do* rather than a button you press.
Start talking and the sentence being spoken stops mid-word, the rest of the
reply is abandoned — including the sentence already being synthesized ahead —
and what you said becomes the answer. The 300 ms pre-roll means the words that
triggered the interruption are part of the recording; you never repeat yourself.

**The microphone is genuinely open while Vox is speaking.** That is a real
widening of when the mic is live, so it is reported rather than hidden: the
menu-bar glyph turns red, the panel reads **Speaking · cut in**, and both
`/health` and `diagnostics(section="privacy")` carry `barge_in_enabled` and
`mic_armed_for_barge_in`.

There is no acoustic echo cancellation. Playback is an opaque `afplay`
subprocess, so no reference signal exists to subtract, which means on laptop
speakers the microphone genuinely hears Kokoro. The gate is what stops that
from self-triggering:

- The noise floor rises fast enough to swallow steady speaker bleed.
- Speech must sit well above that floor, and be sustained rather than a
  transient.
- The turn plays slightly quieter while armed, widening the gap.
- A barge-in whose transcript comes back empty is treated as silence, never as
  something you said. A hallucinated utterance built from Vox's own voice would
  be worse than the interruption.

Run `vox barge-in calibrate` before enabling it. It measures Kokoro's bleed
against your voice on your hardware and prints the margin that follows from the
numbers — including an honest verdict when the gap is too small to gate on.

## The companion (dead-air tier)

Off by default. `VOX_COMPANION_ENABLED=1` gives Vox a second, faster voice for
the 25–40 seconds an agent spends working. The agent calls `companion` with a
brief *before* it goes quiet; the companion speaks it in its own Kokoro voice
and then holds the conversation until the agent comes back.

**It never answers anything about your work.** Scope is a whitelist decided in
`intents.companion_may_answer` — small talk, acknowledgements, "still there?".
Anything that looks like a file, a command, a question about the code, or is
simply long enough to be a real request escalates immediately and returns the
full transcript, so the agent picks up without you repeating yourself. The
heuristic is deliberately biased toward escalating: over-escalating costs a
round trip, under-escalating means a model that has never seen your project
answers a question about it confidently.

**Where the network call happens.** Vox opens no outbound sockets — that stays
literally true. A companion turn shells out to `grokctl ask`, which already
owns the xAI OAuth credential, and *it* makes the call. `diagnostics(section=
"privacy")` reports both halves rather than hiding the second one: `local_only`
stays `true` and a `companion` block names the backend and whether egress is
enabled.

Measured: ~2.5s per companion turn end to end, against 25–40s for a full agent
turn. It is a courtesy, not a dependency — a missing or failing backend
escalates back to the agent rather than taking the voice turn down.

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
