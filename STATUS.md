# Vox MCP — status

Local-only voice runtime (Whisper STT + Kokoro TTS) exposed as an MCP server on
`127.0.0.1:8766`. Multiple agents call it concurrently all day.

## Where it lives

The repo is `~/vox-mcp`, pushed to the private `AlqattanDev/vox-mcp`. The launch
agent (`~/Library/LaunchAgents/com.vox.runtime.plist`, via `VOX_RUNTIME`) points
at `~/vox-mcp/.venv/bin/voxd`.

The real package is `src/voxmcp/`. The vendored `voice_mode/` tree is a frozen
upstream compatibility layer — packaged for the wheel build, not edited.

## What works

- **Concurrent agents queue.** Overlapping audio turns wait their turn in FIFO
  order instead of raising `BusyError`, bounded at 30s. `cancel`, `stop`, and
  `pause` drain the queue. Verified live: two agents called at the same instant,
  one spoke at +4.4s and the other at +7.8s, neither failed.
- **Per-project identity.** `?agent=<name>` in the MCP URL names the speaker,
  independent of the host-keyed lease (which still follows the host so it
  survives reconnects). `agent="..."` works as a tool param for callers that
  cannot set a URL.
- **Per-agent voices.** Auto-assigned from the local English Kokoro voices by a
  sha256 of the label, persisted to `~/.vox/agents.json` so they never drift.
  Hand-written entries are honoured verbatim. `default` keeps `af_sky`.
- **Cancel answers the caller.** Cancelling mid-speech returns
  `{"status": "cancelled"}` instead of leaving the tool call unanswered. A host
  dropping the request still propagates `CancelledError`, as cooperative
  cancellation requires.
- **Observability.** `voice_session(status)` reports the active agent, the
  queue, and the caller's resolved voice; `voice_registry` lists the mapping.
  Queue transitions land in `~/.vox/state/events.jsonl`.
- **Whisper on `small`.** One server (`com.vox.whisper`, `127.0.0.1:2022`).
  The model path is baked into the plist from `InstallerPaths` —
  `voicemode.env`'s `VOICEMODE_WHISPER_MODEL` is read only by the vendored
  start script, not by the agent that serves.
- **Loud rooms endpoint instead of recording forever.** The adaptive noise
  floor used to learn only from non-speech frames, so ambient above the
  threshold (fan, AC, music) read as endless speech and every listen ran to the
  5-minute cap. Now a frame the WebRTC VAD rejects always feeds the floor at the
  normal rate, and VAD-accepted frames drift it up slowly
  (`noise_rise_smoothing`, ~20s time constant) as a backstop when the VAD is
  fooled. Real speech is safe: inter-word gaps pull the floor back down.
  Limitation: audible background music still degrades listening — the VAD votes
  speech on music, so only the slow backstop applies. Silence the room or
  expect ~25s turns.
- **The installer removes legacy plists, it does not just unload them.**
  `plan.delete_targets` lists every `STALE_LABELS + LEGACY_BACKEND_LABELS` plist
  and `activate()` deletes each one after booting the job out, so launchd's
  login rescan cannot revive it via `RunAtLoad`. The delete is gated on file
  presence, never on `loaded` — an unloaded plist is exactly the one that
  survives to the next login. Every delete target is a subset of
  `backup_targets`, so an activation failure restores the files and their loaded
  state; `test_activation_failure_restores_deleted_legacy_plists` holds that
  line. Dry-run `vox install` prints the doomed paths.

## Memory

Measure with `footprint -p <pid>`, never `ps`/RSS: whisper mmaps its model and
kokoro uses Metal unified memory, so RSS under-reports by up to 65x (whisper
reads 9 MB in `ps` against a true 591 MB).

| service | now | peak |
|---|---|---|
| `com.vox.kokoro` (TTS) | ~2050 MB | ~2400 MB |
| `com.vox.whisper` (`small`) | ~590 MB | ~740 MB |
| `com.vox.runtime` (`voxd`) | ~73 MB | ~73 MB |
| **total** | **~2.7 GB** | **~3.2 GB** |

Kokoro is the dominant cost — a PyTorch/MPS process, ~4x whisper. STT model
choice is a rounding error next to it.

Tests: `.venv/bin/python -m pytest tests/` — **186 passing**.

## Wired up

Vox is configured globally in `~/.claude.json` (no `?agent=`, so anything
unlisted is `default` on `af_sky`). These projects have a local-scope override
that names their speaker:

| project | agent | voice |
|---|---|---|
| `~/infra` | `infra` | `af_jessica` |
| `~/fable-5/games/mobilescape` | `mobilescape` | `am_fenrir` |
| `~/vox-mcp` | `vox` | `am_liam` |
| `~/.claude` | `claude` | `af_bella` |

Add another with, from inside the repo:

```bash
claude mcp add --scope local --transport http vox \
  "http://127.0.0.1:8766/mcp?agent=<name>"
```

## Next steps

- `small` is a real accuracy step down from `large-v3-turbo` on accents and
  proper nouns. `medium` is 1.5 GB and buys little over large — if `small`
  proves too lossy the choice is living with it or one server at ~1.6 GB
  (`ggml-large-v3-turbo.bin` is still on disk; revert `InstallerPaths` and the
  live plist).
- Kokoro is the only remaining lever worth pulling on memory (~2.1 GB of a
  2.8 GB stack). Nothing is in flight on it.
- webrtcvad votes speech on music and some broadband noise, at every
  aggressiveness, at 48k and 16k alike. A Silero VAD (whisper.cpp already ships
  `silero` models and a `download-vad-model.sh`) would endpoint through music
  properly; that is the next real upgrade to listening.
