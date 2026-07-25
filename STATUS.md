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
  `diagnostics(privacy)` rather than interrupting itself. Plug in AirPods and
  it arms with nothing to configure. `VOX_BARGE_IN_REQUIRE_HEADPHONES=0`
  overrides deliberately. On speakers, the **Reply button / ⌃⌥⌘R** is the
  interruption path and needs no acoustics.
- **Barge-in mechanism (off by default).** `VOX_BARGE_IN_ENABLED=1` opens the mic
  *during* playback: start talking and the current sentence dies mid-word, the
  remaining spans and the one being synthesized ahead are abandoned, and the
  pre-roll makes your opening words the reply — one capture serves both the
  detection and the answer. There is no AEC (playback is an opaque `afplay`
  subprocess, so no reference signal exists), so the gate carries it: a short
  0.8s noise window re-reads the room fast enough that speaker bleed becomes
  the new floor within a sentence, the margin above it doubles, onset needs
  0.3s of sustained speech, and the turn ducks slightly. An empty
  transcript after a barge-in is reported as silence, never as a user
  utterance. `vox barge-in calibrate` measures bleed vs voice on any given
  hardware and says plainly when the gap is too small. Honest while armed: `microphone_open`, `mic_armed_for_barge_in`,
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
  **Reply** button (idle primary) and the permission-free global hotkey **⌃⌥⌘R**
  (Carbon `RegisterEventHotKey`) grab the mic and answer whoever last spoke —
  `reply` control → `engine.reply` auto-targets `last_spoken_agent` (tracked in
  `_speak_locked`, exposed on `/health`) and delivers via the note path, so it
  surfaces as that agent's `undelivered_heard` on its next turn. The agent-picker
  note moved into **More…**. Inherent limit: a reply after a full sign-off waits
  for the agent's next action (host-injection is the `deliver_text` PLAN item).
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

Tests: `.venv/bin/python -m pytest tests/` — **276 passing**.

**Slash commands** (in `~/.claude/commands/`, global): `/speak` reads the
agent's last reply aloud (no mic); `/listen` opens the mic for one utterance
without arming a persistent session.

**Deploy after any change:** `zsh scripts/install_macos_app.sh`. It builds,
installs to `~/Applications/Vox.app` **cleanly** (removes the old app first —
`cp -R` into an existing dir nests the app and leaves launchd running the stale
binary), guards against nesting, and restarts `com.vox.runtime`. The editable
Python runtime picks up code changes on that restart.

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

- **Barge-in.** The whole mechanism has only ever run against fakes. It
  self-disables on this machine's speakers (correctly — see above), so proving
  it needs one session wearing headphones. `PLAN.md` step 1.
- **Companion beyond two turns.** Live-verified for one answer plus one
  escalation. The `budget_turns` loop, the STOP/PAUSE intents inside it, and
  `voice_survey(agent="companion")` as the interview path are untested aloud.
- **The turn contract under pressure.** It cut the mean from 30.2 s to 18.1 s,
  but one exchange still ran 26 s. It is a prompt, not a mechanism, and nothing
  enforces it.

Three of this session's worst bugs — an echo gate that did nothing, a floor
that cut people off mid-sentence, a scope check that answered code questions —
were all already marked done and were all found by *using* the thing, never by
the suite. Verify aloud before believing green.

## Next steps

- **Type-while-listening fusion** (see `PLAN.md`). When Ali types while the mic
  is open, deliver the typed text immediately instead of waiting out the listen.
  Needs a host-side Claude Code integration; the 75s cap already softens the
  wasted-turn wait.
- **Speak while the agent is still composing.** The streamed-TTS win is inside
  one `speak` call; starting speech before the reply is fully written is a
  client concern (Claude Code sends the whole message in one tool call). Would
  need the agent/skill to emit sentence-level `speak` calls as it writes.
- Silero VAD for music-proof endpointing.
- Whisper hallucination filter (drop `[wind]`/`[music]`-only transcripts).
- If `small` is too lossy on accents: revert model path to
  `ggml-large-v3-turbo.bin` (~1.6 GB).
- Kokoro memory is the only large remaining cost lever.
- System-wide dictation (Wispr replacement) is out of scope for this runtime.
