# Vox MCP — status

Local-only voice runtime (Whisper STT + Kokoro TTS) on `127.0.0.1:8766`.
**Shared session** — no exclusive owner. Grok, Fable, Claude, Codex all join
the same session; each keeps its own agent voice; audio is FIFO-queued only.

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
- **Snappier endpointing.** Trailing-silence 1.6s (was 1.2 — stops cutting off
  mid-thought); a single utterance caps at 75s (was 300 — kills the multi-minute
  "listens forever" hang). Env: `VOX_TRAILING_SILENCE_SECONDS`,
  `VOX_MAX_UTTERANCE_SECONDS`. These are the MCP tool defaults too, which govern
  live usage.
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

## Memory

| service | now | peak |
|---|---|---|
| `com.vox.kokoro` | ~2050 MB | ~2400 MB |
| `com.vox.whisper` (`small`) | ~590 MB | ~740 MB |
| `com.vox.runtime` | ~73 MB | ~73 MB |
| **total** | **~2.7 GB** | **~3.2 GB** |

Tests: `.venv/bin/python -m pytest tests/` — **205 passing**.

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
