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
- **Menu bar (adaptive).** A glanceable SF Symbol glyph, not a text string:
  a **red mic only when `microphone_open` is truly set** (never off stale
  session state), so idle/speaking never read as "hot." **Left-click while the
  mic is live ends the turn**; right-click / mic-closed click opens the panel.
  Polls every 0.4s so the glyph never lags.
- **Decluttered panel.** Mode is three buttons (Talk / Narrate / Dictate, active
  one pushed in). One turn action: **Stop listening**. **Repeat last speech**
  replays the agent's last clip. The old Stop / Start / Resume / Pause / Cancel
  pile-up collapsed to one contextual **Turn Vox off/on** in **More…**, with
  **Restart Vox** as the sole recovery. Sleep/inactivity auto-pause unchanged.
- **Leave a note (`note` control / panel button).** Speak one utterance without
  waiting for the agent to open the mic — the runtime records it and holds it
  **undelivered**; the agent claims it on its next turn (`claim_undelivered` /
  `undelivered_heard`). Fire-and-forget over HTTP (returns 202); the earcon and
  red glyph cue you to talk. The panel shows when a note is waiting.
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

Tests: `.venv/bin/python -m pytest tests/` — **197 passing**.

**Slash commands** (in `~/.claude/commands/`, global): `/speak` reads the
agent's last reply aloud (no mic); `/listen` opens the mic for one utterance
without arming a persistent session.

**After menu-bar changes, rebuild + reinstall the app** (the runtime restarts):
`zsh scripts/build_macos_app.sh` then the install step. The Python runtime
changes take effect on the next `com.vox.runtime` restart.

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
