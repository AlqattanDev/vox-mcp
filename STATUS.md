# Vox MCP — status

Local-only voice runtime (Whisper STT + Kokoro TTS) on `127.0.0.1:8766`.
**Shared session** — no exclusive owner. Grok, Fable, Claude, Codex all join
the same session; each keeps its own agent voice; audio is FIFO-queued only.

## Settings

`~/.vox/settings.json`, edited with **`vox set VOX_COMPANION_ENABLED=1`**
(`vox set` alone prints it; `--unset NAME` removes; restarts the runtime unless
`--no-restart`). A real environment variable still wins, so
`VOX_BARGE_IN_ENABLED=1 voxd` overrides for one run.

This exists because launchd hands the runtime **none** of the shell environment
and does not reliably pass `launchctl setenv` — every `VOX_*` knob was
unreachable in the installed deployment, and nothing set that way survived a
reboot. The file is the configuration that actually arrives. Non-`VOX_`-prefixed
keys are ignored rather than injected into the process environment.

## Where it lives

Repo `~/vox-mcp` → private `AlqattanDev/vox-mcp`. Launch agent
`com.vox.runtime` runs `~/Applications/Vox.app` → `~/vox-mcp/.venv/bin/voxd`.
Package is `src/voxmcp/`. Vendored `voice_mode/` is frozen compatibility.

## What works

- **Shared session (no ownership).** `LeaseManager` records `last_actor` only.
  `_claim` never raises “belongs to…”. Handoff/takeover are legacy: takeover
  cancels the active turn + drains the queue; handoff is a shared no-op.
  BusyError means queue timeout or privacy pause — not another agent “owns”
  voice.
- **Concurrent agents queue.** FIFO, 30s wait bound. Cancel/stop/pause drain.
- **Per-agent voices.** `?agent=` / `agent=` + `~/.vox/agents.json`.
- **Cancel answers the caller.** Mid-speech cancel returns `status: cancelled`.
- **Finished STT survives host drop.** `~/.vox/state/last_heard.json` is
  written the instant Whisper finishes. Host `CancelledError` after STT
  returns the transcript (`delivered_via: cancel_recovery`).
  `voice_session(claim_undelivered)` one-shots recovery. Status/health expose
  `undelivered_heard` (no full transcript on health).
- **Interrupted speech survives a crash.** A host/transport drop is
  `INTERRUPTED`, distinct from a deliberate user `CANCELLED`: the captured audio
  is kept and written to the recovery wav, so a mid-utterance interruption is
  recoverable via `transcribe(latest=true)`. User cancel still discards for
  privacy.
- **Listen earcons.** A rising blip marks the mic opening (talk now), a falling
  blip marks it closing (stopped) — the start tone plays before capture so it
  can't leak in. Off via `VOX_EARCONS=0`.
- **Streamed TTS.** Long replies speak sentence-by-sentence; the next span
  synthesizes while the current one plays, so audio starts after the first
  sentence. Off via `VOX_STREAM_TTS=0`.
- **The noise floor is read from the room, every few seconds.** No calibration,
  no stored level, nothing to re-run when you move. The floor is the 10th
  percentile of a rolling 3 s window of raw loudness (speech has gaps; the
  quietest tenth is the room), and the rise real speech must clear is scaled
  from how far the room wanders above it — bounded 3–15 dB. Read from *raw*
  levels, never from frames a classifier already labelled: labelling first is
  circular, so in a loud room every frame reads as speech and the floor never
  rises to meet it. **During an utterance the floor may only fall** — talk for
  longer than the window without pausing and its quietest tenth stops being the
  room and becomes you, so the floor climbs into your voice and the turn
  endpoints mid-word (Ali hit exactly this live). A *stationary* window is
  exempt, since a flat level is a drone and must still be learned; speech
  varies syllable to syllable and a fan does not. Verified across four
  simulated rooms nearly 40 dB apart (silent bedroom → café), plus live: one
  word **7.24 s → 1.6 s capture** with nothing configured, beating the
  hand-tuned value it replaced. `VOX_MINIMUM_SPEECH_DBFS` is a hard backstop,
  not part of setup.
- **Endpointing scales with the utterance.** Trailing silence runs 0.6s for a
  short answer and 1.6s for a long one, interpolated over 1.5s–3.0s of measured
  *speech* (not wall time, so pausing to think keeps a long answer on the long
  deadline). A one-word "keep" stops making you wait a second and a half; a
  rambling turn keeps every bit of the patience it had. A single utterance caps
  at 75s. Env: `VOX_TRAILING_SILENCE_SECONDS`,
  `VOX_SHORT_TRAILING_SILENCE_SECONDS`, `VOX_SHORT_UTTERANCE_SPEECH_SECONDS`,
  `VOX_LONG_UTTERANCE_SPEECH_SECONDS`, `VOX_MAX_UTTERANCE_SECONDS`. Lowering the
  ceiling below the floor clamps instead of raising.
- **Barge-in needs headphones, and knows it.** Measured on this MacBook: Kokoro
  through the built-in speakers returns into the mic at **−22 dBFS p90 / −17.6
  peak**, while Ali's own voice peaks at **−29.8** (median −46). *The user is
  ~24 dB quieter than his own echo* — no threshold separates them, so on
  speakers this is arithmetic, not tuning. Barge-in therefore checks the
  default output device and **declines to arm** unless it is recognisably
  headphones, reporting `barge_in.reason = "shared_output"` in
  `diagnostics(privacy)` rather than interrupting itself.
  `VOX_BARGE_IN_REQUIRE_HEADPHONES=0` overrides deliberately. On speakers, the
  **Reply button / a ⌘§ tap** is the interruption path and needs no acoustics.
  The device list is **re-read on every availability check** — PortAudio
  snapshots devices at process start, so headphones plugged in after the runtime
  launched were invisible and barge-in kept refusing on the speakers it no
  longer used. Skipped while a capture stream is open, because reinitializing
  PortAudio under a live `InputStream` would take the turn with it.
- **Barge-in is verified live on headphones** (2026-07-26, HUAWEI FreeClip 2).
  Recognition is by device *name*, so these open-ear buds are not on the
  headphone list and needed the override — but they measured **+27.6 dB in the
  user's favour** (bleed p90 −63.3 armed, voice median −37.2), the near-exact
  inverse of the −24 dB on speakers, a ~52 dB swing. The reason is causal, not
  luck: connecting them moves *both* input and output to the earbuds, so the mic
  in use sits inches from the mouth and isolated from the driver.
  Three long replies with the mic open produced **zero false interrupts**; a
  deliberate interruption returned `spoken.status: "barge_in"` with the
  transcript **"Pineapple is something I don't like actually."** — the planted
  opening word survived, which is the pre-roll splice proven end to end.
  **Cost of it on Bluetooth:** opening the mic drags the earbuds from A2DP into
  the HFP call profile (their input resolves at 16 kHz, the MacBook mic at 48),
  so an armed reply is louder and narrower-band for its whole duration. That is
  the price of barge-in working at all on earbuds, and it is why the mic closing
  mid-reply used to be audible as a volume collapse.
- **Barge-in mechanism (off by default).** `VOX_BARGE_IN_ENABLED=1` opens the mic
  *during* playback: start talking and the current sentence dies mid-word, the
  remaining spans and the one being synthesized ahead are abandoned, and the
  pre-roll makes your opening words the reply — one capture serves both the
  detection and the answer. There is no AEC (playback is an opaque `afplay`
  subprocess, so no reference signal exists), so the gate carries it: a short
  0.8s noise window re-reads the room fast enough that speaker bleed becomes
  the new floor within a sentence, the margin above it doubles, onset needs
  0.3s of sustained speech, and the turn ducks slightly. **The armed mic has no
  onset timeout** — what ends it is the reply finishing, not a clock. It used to
  inherit the ordinary onset timeout and close itself partway through a long
  answer: measured dying at **15.1s of a 72s reply**, leaving 79% of it silently
  uninterruptible, with the vanishing menu-bar mic badge as the only clue. An
  empty
  transcript after a barge-in is reported as silence, never as a user
  utterance. `vox barge-in calibrate` measures bleed vs voice on any given
  hardware and says plainly when the gap is too small; `--countdown N` replaces
  the keypress so an agent can drive it. It recommends
  **`VOX_BARGE_IN_VAD_MARGIN_DB`**, the *floor* of the required rise — the rise
  is `clamp(k × spread, floor, ceiling)`, so only the floor can guarantee a
  separation rather than merely permit one. When the gap is too small it
  recommends **nothing at all**, because no setting invents a difference that
  is not in the acoustics. Ali's own hardware needs no tuning: zero false
  interrupts across three long replies on defaults. Honest while armed: `microphone_open`, `mic_armed_for_barge_in`,
  `barge_in_enabled` on `/health` and `diagnostics(privacy)`; the glyph goes
  red and the panel reads "Speaking · cut in".
- **Companion tier (off by default).** `VOX_COMPANION_ENABLED=1` + the
  `companion` tool: hand over a brief before a long stretch of work and a
  second Kokoro voice keeps the user company instead of leaving dead air.
  **Verified live end to end**: "So how you doing man?" → answered in its own
  voice in **3.1 s**; the next turn mentioned code and escalated without
  calling the backend at all. **Never answers anything about the project** —
  `companion_may_answer` requires a positive small-talk *signal*, so a
  greeting does not launder a work question riding along with it, and anything
  unmarked escalates with the full transcript attached.
  Backend is `grokctl ask` on Ali's xAI OAuth (no metered key, no new
  credential), so **voxmcp still opens zero outbound sockets** and
  `config.local_only` stays an unqualified true; `diagnostics(privacy)` carries
  a `companion` block naming the backend and egress. A missing or failing
  backend escalates rather than stalling.
- **Voice turn contract.** The runtime owns under 1.5s of a spoken turn and the
  agent owns the rest (measured table below). That gap is desk work done inside
  a conversation, so the contract ships where hosts read it — MCP server
  instructions, the `voice_mode` prompt, and the `converse` tool description:
  one or two sentences, no tool calls between turns, detail on screen not in
  the speaker, announce work before going quiet. See `docs/EXPERIENCE.md`.
- **IO modes.** `talk` (default, both) · `narrate` (agent speaks, no mic) ·
  `dictate` (listen only, TTS skipped). Panel cycles; `voice_session`
  `set_mode` / `cycle_mode`; persisted in `~/.vox/state/io_mode`.
- **Menu bar glyph (baked colour).** A glanceable SF Symbol: a **red mic only
  when `microphone_open` is truly set**, so idle/speaking never read as "hot."
  That flag tracks the **gate**: red means audio can reach Whisper, which is
  what the user actually cares about. `/health.stream_open` reports the device
  separately, and now tracks the turn rather than the session.
  **Left-click while the mic is live ends the turn**; right-click / mic-closed
  click opens the panel. The glyph colour is **painted into the image pixels**
  (non-template, palette-tinted from `AppleInterfaceStyle`), not left to the
  system to tint — on macOS Tahoe an accessory app's `effectiveAppearance`
  resolves to Aqua even in Dark mode, so template auto-tint *and*
  `contentTintColor` both paint it black and it vanishes on the dark bar. Re-baked
  on the `AppleInterfaceThemeChangedNotification` light/dark switch.
- **Premium panel (state-driven).** A **hero row** (state dot + "Vox" + one state
  word) over a **live waveform** (`LevelMeterView`) that reacts to your real mic
  level — the runtime reports `mic_level` (0..1, derived from capture dBFS) over
  `/health`, and the panel polls at 0.08s while open (0.4s closed) so the bars
  move as you speak. Modes are one **segmented control** (Talk / Narrate /
  Dictate). **One contextual primary button**: *Stop listening* (red) when the
  mic is hot, *Turn Vox on* / *Resume Vox* when it's down, *Reply* when idle.
  Repeat + More… shrink to a small footer. `More…` holds the agent-picker note,
  Turn-off/Restart/Open-folder controls. Sleep/inactivity auto-pause unchanged.
- **User-decided replies.** Turn-taking is the user's call, not only the
  agent's. **(1) Reply window:** `converse`/`listen` take `onset_timeout` — a
  short value (~4s) opens the mic for a beat after the agent speaks and returns
  `no_speech` fast on silence, so a conversational turn offers a reply without a
  hanging mic. The skill tells agents to end back-and-forth turns this way and
  reserve bare `speak` for a true sign-off. **(2) Reply on demand:** a panel
  **Reply** button (idle primary) and a tap of the permission-free global hotkey
  **⌘§** (Carbon `RegisterEventHotKey`) grab the mic and answer whoever last spoke —
  `reply` control → `engine.reply` auto-targets `last_spoken_agent` (tracked in
  `_speak_locked`, exposed on `/health`) and delivers via the note path, so it
  surfaces as that agent's `undelivered_heard` on its next turn. The agent-picker
  note moved into **More…**. Inherent limit: a reply after a full sign-off waits
  for the agent's next action (host-injection is the `deliver_text` PLAN item).
- **Type instead of talking, without waiting the mic out.** `deliver_text` ends
  an in-flight listen and makes the supplied string the turn:
  `vox control deliver-text "..."`, the `deliver_text` action on
  `voice_control`, or a POST to `/control`. A running listen holds the host's
  turn open, so a message typed while the mic was live used to sit queued until
  the listen timed out — the user waiting out a microphone they had already
  decided not to use, spending the turn on nothing. Verified live: the listen
  returned the typed text with `backend: delivered_text` and **`stt_ms: 0`**.
  Audio is discarded exactly as a cancel discards it, because choosing to type
  means whatever the room said meanwhile is not the user's turn. The text runs
  the same path a transcript does, so it is recorded for recovery and
  classified for intent — typing "stop" ends the session just like saying it.
  A no-op when nothing is listening, so the caller sends the message normally.
- **Addressed notes (`note` control / panel button).** Speak one utterance
  without waiting for an agent to open the mic. The button first pops an agent
  picker (each agent = a project/voice); the note is **addressed to that agent**
  and surfaces as `undelivered_heard` only in *its* status, so only that agent
  claims it — not whoever polls first. `NotesStore` (`~/.vox/state/notes.json`)
  holds one pending note per agent, so notes to different agents coexist. Empty
  target = broadcast (any agent). Fire-and-forget over HTTP (202); the earcon +
  red glyph cue you to talk. Health exposes `agents` + `notes_waiting`; the panel
  names who has a note waiting.
- **Loud rooms endpoint.** Adaptive floor + `noise_rise_smoothing` backstop.
  Music still degrades VAD (Silero later).
- **Whisper `small`.** One server `com.vox.whisper` `:2022`.
- **No empty logs theater.** Installer does not create `~/.vox/logs/*`.
  Process stdout/stderr → `/dev/null`. Audit trail: `~/.vox/state/events.jsonl`.

## Measured turn latency (2026-07-25, live)

Who owns the seconds in a spoken turn, from `~/.vox/state/events.jsonl`:

| Phase | Measured | Owner |
|---|---|---|
| Kokoro synth, first span | 0.55–0.89 s | vox |
| Whisper STT | 257–661 ms | vox |
| Trailing silence | 0.6 s short / 1.6 s long | vox |
| **Agent turn** (you stop → next word) | **9.2 / 26.2 / 18.9 s** (mean 18.1) | the agent |

Baseline before the voice turn contract was 25.4 / 40.8 / 24.3 s (mean 30.2).
So the contract took roughly a third off the mean and the runtime owns under
1.5 s of a turn — but 26 s on one exchange says it is discipline, not a fix.
The remaining lever is the agent behaving, not the runtime.

Endpointing, same session, one word: **7.24 s → 1.60 s** (0.4 s of it speech)
once the floor was read from the room. A ramble still gets patience scaled to
its length — 1.88 s of speech drew 0.86 s of trailing silence, 12.5 s drew the
full 1.6 s — and is never cut off mid-thought.

## Memory

| service | now | peak |
|---|---|---|
| `com.vox.kokoro` | ~2050 MB | ~2400 MB |
| `com.vox.whisper` (`small`) | ~590 MB | ~740 MB |
| `com.vox.runtime` | ~73 MB | ~73 MB |
| **total** | **~2.7 GB** | **~3.2 GB** |

Tests: `.venv/bin/python -m pytest tests/` — **387 passing**.

**Slash commands** (in `~/.claude/commands/`, global): `/speak` reads the
agent's last reply aloud (no mic); `/listen` opens the mic for one utterance
without arming a persistent session.

**Deploy after any change:** `zsh scripts/install_macos_app.sh`. It builds,
installs to `~/Applications/Vox.app` **cleanly** (removes the old app first —
`cp -R` into an existing dir nests the app and leaves launchd running the stale
binary), guards against nesting, and restarts `com.vox.runtime`. The editable
Python runtime picks up code changes on that restart.

The build **fails without a persistent codesigning identity** (currently Apple
Development, team `YN9839UZF5`). That is deliberate: macOS pins Accessibility
to the code signature, and the old ad-hoc fallback minted a new cdhash every
build — silently revoking dictation and read-aloud on every deploy with nothing
to explain why. Set `VOX_CODESIGN_IDENTITY`, or `VOX_ALLOW_ADHOC_SIGN=1` to
build anyway and re-grant by hand.

## Wired agents

| project | agent | voice |
|---|---|---|
| `~/infra` | `infra` | `af_jessica` |
| `~/fable-5/games/mobilescape` | `mobilescape` | `am_fenrir` |
| `~/vox-mcp` | `vox` | `am_liam` |
| `~/.claude` | `claude` | `af_bella` |

```bash
claude mcp add --scope local --transport http vox \
  "http://127.0.0.1:8766/mcp?agent=<name>"
```

## Not yet verified against real audio

Honest list — these are built, committed, and green in tests, but no human has
used them:

- **Companion beyond two turns.** Live-verified for one answer plus one
  escalation. The `budget_turns` loop, the STOP/PAUSE intents inside it, and
  `voice_survey(agent="companion")` as the interview path are untested aloud.
- **The turn contract under pressure.** It cut the mean from 30.2 s to 18.1 s,
  but one exchange still ran 26 s. It is a prompt, not a mechanism, and nothing
  enforces it.

Verifying barge-in aloud cost one session and found **three more** bugs that the
276-test suite was green on: a session that wedged in `SPEAKING` forever after a
cancel while armed (bricking every later turn), an armed mic that closed itself
15s into a 72s reply, and a device list that never noticed headphones plugged in
after launch. One of them had an existing test that asserted the microphone
closed but never that the state machine recovered — it passed throughout. Verify
aloud before believing green, and assert the state, not just the device.

## The voice suite

**One capture stream per turn, with a gate in front of it.** Vox used to open
and close an `InputStream` twice per turn — the armed barge-in capture, then a
fresh one ~130 ms later. On the FreeClip 2 every open emits a broadband
transient about **240 ms after the stream engages**, ~24 dB above the initial
threshold, decaying over ~350 ms. WebRTC endorses broadband noise and 60 ms of
it satisfies `speech_start_s`, so turns opened on a pop nobody made, endpointed
at ~0.9 s, and came back `stt.non_speech` — while the words actually spoken
landed in the dead air between two phantom windows. Reproduced on demand from a
cold start with no TTS playing; echo was refuted as the cause.

`PersistentCaptureSource` (`src/voxmcp/capture_source.py`) drops frames **in the
realtime callback** whenever no capture holds the gate — nothing queued,
buffered, or classified between turns. Turns run through the existing
endpointing path via `capture_from_frames`; the frame generator ends itself on
the control flags, so the exhaustion branch resolves the right stop reason with
no new state-machine code.

**The device's lifetime is the turn's, not the session's.** macOS lights its
orange microphone indicator for an open input *device* and knows nothing about
our software gate, so a stream that outlived the turn was an indicator burning
with no honest meaning — and Ali, correctly, read a permanently lit dot as being
listened to all day. `_ensure_source` now returns a **hold** the caller must
give back; the armed capture, the listen, a turn-key turn and a dictation each
take one, and when the last is released the device closes after
`VOX_STREAM_IDLE_RELEASE_SECONDS` (**2.0**). The linger is not a grace period
for idling: it exists only so the hand-off from armed playback to the listen
that follows it inside one turn does not close and reopen the device between
them. Verified live end to end: idle → tap → `device=True gate=False` (warm-up)
→ `gate=True mic=True` → close → **released**, with exactly one
`capture.stream_opened` and one `capture.stream_closed` per turn.

Dictation used to hand a *shared* stream back still open — it closed only one it
had opened itself — so a single hold-to-talk press left the device live until the
next pause or stop. That was the lit dot Ali saw, straight after the one
Accessibility-granting press the setup asked for. Both the key-released and
max-duration paths now release.

The open guard is **1.0 s, not 0.5 s**: half a second was measured to be too
short — onset still fired 513 ms after open and reproduced the flicker exactly.
`_capture_once` waits the remainder out *before* the start cue, so the rising
blip means "I can hear you now" rather than "soon", and so the cue itself —
played into the same headset the mic is in — cannot bleed into the recording.
**Dictation deliberately skips it**: the raw path has no onset detection for a
transient to fool, the pop just reaches Whisper as a click, and waiting would
cost a full second of dead air on a key the user is already talking into.

**The reply window is 5 s** (`onset_timeout_s`, was 15). This is how long the mic
stays open after the agent stops speaking. Fifteen seconds was fifteen seconds in
which the room could interrupt an agent that had gone back to work. Still `None`
on the two paths where a clock would be wrong: an armed barge-in mic, and a turn
the user opened with the key.

**Hotkeys: two combos on one key**, both permission-free Carbon registrations on
`kVK_ISO_Section` (verified to map to "§" under this machine's ABC layout, and
both combos register `noErr`).

| | |
|---|---|
| **⌘§** tapped | Talk to the agent — tap again to send. Tapped when nothing is listening it starts a turn addressed to whoever last spoke |
| **⌘§** held | Dictate at the cursor; release injects |
| **⇧⌘§** | Read the selection aloud |

⌘ alone registers as freely as any three-modifier combo, so two keys is all a
permission-free hotkey costs. Tap and hold
are told apart by a **350 ms** threshold: nothing happens on key-down, because
what the press means is not yet known. A separate reply hotkey no longer exists —
`_gate_open` already starts a turn addressed to `last_spoken_agent` when nothing
is listening, so it was the same action twice. The Carbon handler had to be
rewritten before any of this: it installed one `EventTypeSpec` and ignored all
three of its arguments, so it could tell neither which hotkey fired nor whether
the key went down or up.

**The floating pill** (`macos/VoxHUD.swift`) is a capsule at bottom-centre that
exists *only* while something is happening, so its presence is the signal and its
absence is the reassurance. Four states, colour-coded because "who gets these
words" is the thing to see at a glance: **warming** (device up, gate still shut —
makes the 1.0 s guard read as deliberate rather than as a dead key press),
**listening** (red, an agent turn), **dictating** (teal, going to the cursor),
**speaking** (blue). It is a `.nonactivatingPanel` ordered front with
`orderFrontRegardless()` and never `makeKeyAndOrderFront` — taking key status
would move the insertion point out of the field dictation is about to paste into.
Speaking animates a synthetic pulse rather than a waveform, because playback is
an opaque `afplay` subprocess with no readable level and a fake waveform reading
as mic input would be the same dishonesty as the lit dot. `VOX_HUD=0` disables.

**Dictation and read-aloud need no MCP client and no voice session.** Dictation
uses `capture_raw_from_frames` — a genuinely raw path, not the endpointer in a
costume. The first attempt reused `AdaptiveCaptureState` with a classifier that
voted speech on every frame; that is wrong because `_classify` still gates a
positive vote on the adaptive energy threshold, so a quiet start is dropped.
Live, it captured 5.2 s and returned nothing. Injection is clipboard + ⌘V in
Swift (per-character events break on Arabic; `AXSelectedText` writes are
read-only in Chromium/Electron). **Read-aloud never opens the microphone** —
structurally, not as a promise: `_speak_locked` takes `barge_in=False` by default
and only `converse`'s reply passes `True`. Verified live across a whole spoken
reply: zero `capture.stream_opened`, zero `barge_in.armed`.

**Everything heard is left on the clipboard.** Dictation stops restoring the
previous pasteboard, and a spoken transcript is copied from `_record_heard` via
`pbcopy` (`VOX_CLIPBOARD_TRANSCRIPT=0` opts out). A paste into a surface with no
editable field silently goes nowhere and the words were simply gone; now the
fallback is always ⌘V. Skipped for `deliver_text`, which the user typed and
already has.

**Accessibility is required** for ⌘§ held and ⇧⌘§. macOS pins TCC grants to the
code signature, so `build_macos_app.sh` **fails** rather than falling back to
ad-hoc signing, which would revoke the grant on every install with no error
anywhere. `VOX_ALLOW_ADHOC_SIGN=1` opts out.

**Device note:** the FreeClip cancels its own speaker out of the mic feed, so
playing audio through it to test capture measures −61 dBFS. It is not a usable
loopback — test capture by speaking.

`/control` actions: `gate_open`, `gate_close`, `dictate_start`, `dictate_end`,
`read_aloud`. Knobs: `VOX_PERSISTENT_CAPTURE` (1),
`VOX_STREAM_OPEN_GUARD_SECONDS` (1.0), `VOX_STREAM_IDLE_RELEASE_SECONDS` (2.0),
`VOX_CLIPBOARD_TRANSCRIPT` (1), `VOX_HUD` (1), `VOX_DICTATION_CLEANUP`
(`rules`), `VOX_DICTATION_MAX_SECONDS` (120). `/health` carries `gate_open` and
`stream_open`, which are not the same thing.

## Next steps

- **Verify the voice suite aloud.** Machine-checkable acceptance passed against
  the deployed app on the real FreeClip: the device is closed at rest, one
  `capture.stream_opened` + one `capture.stream_closed` per turn, dictation
  releases the device, read-aloud opens no input device at all through a whole
  spoken reply, and pause/stop/deliver_text unchanged. Still needs a human voice:
  a 60 s turn with long pauses landing as one clean transcript, barge-in still
  firing on earbuds, dictation into Chrome/Notes/Slack/a terminal with the
  transcript still on the clipboard afterwards, Arabic round-tripping, and
  read-aloud spot-checked against numbers, names, and code. Also needs eyes on
  the pill: warming → listening → gone, and a distinct tint while dictating.
  Grant Accessibility on the first ⌘§ hold.
- **Confirm the `UserPromptSubmit` hook fires during a running tool call.** The
  hook is installed (`.claude/settings.json` →
  `scripts/claude_code_deliver_text.sh`) and the script is verified end to end:
  no-op with the mic closed, and with the mic open it delivers and the listen
  returns `backend: delivered_text`. What is unproven is the host half — hooks
  load at session start, so this needs a fresh Claude Code session, and then
  typing while a listen is in flight. If the hook does not fire mid-tool-call
  then `vox control deliver-text` stays the manual path; no poller that guesses
  when Ali is typing.
- **Speak while the agent is still composing.** The streamed-TTS win is inside
  one `speak` call; starting speech before the reply is fully written is a
  client concern (Claude Code sends the whole message in one tool call). Would
  need the agent/skill to emit sentence-level `speak` calls as it writes.
- Silero VAD for music-proof endpointing.
- Whisper hallucination filter (drop `[wind]`/`[music]`-only transcripts).
- If `small` is too lossy on accents: revert model path to
  `ggml-large-v3-turbo.bin` (~1.6 GB).
- Kokoro memory is the only large remaining cost lever.
