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
- **IO modes.** `talk` (default, both) · `narrate` (agent speaks, no mic) ·
  `dictate` (listen only, TTS skipped). Panel cycles; `voice_session`
  `set_mode` / `cycle_mode`; persisted in `~/.vox/state/io_mode`.
- **Menu bar.** Mode cycle + Stop/Start + Cancel this turn. Restart / Open
  folder / Pause under **More…**. Control HTTP timeout 6s (matches mic close).
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

Tests: `.venv/bin/python -m pytest tests/` — **191 passing**.

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

- Silero VAD for music-proof endpointing.
- If `small` is too lossy on accents: revert model path to
  `ggml-large-v3-turbo.bin` (~1.6 GB).
- Kokoro memory is the only large remaining cost lever.
- System-wide dictation (Wispr replacement) is out of scope for this runtime.
