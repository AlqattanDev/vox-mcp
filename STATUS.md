# Vox MCP — status

Local-only voice runtime (Whisper STT + Kokoro TTS) exposed as an MCP server on
`127.0.0.1:8766`. Multiple agents call it concurrently all day.

## Where it lives

The repo is `~/vox-mcp`, pushed to the private `AlqattanDev/vox-mcp`. It used
to run out of a Codex scratch output directory with no version control; the
launch agent (`~/Library/LaunchAgents/com.vox.runtime.plist`, via `VOX_RUNTIME`)
now points at `~/vox-mcp/.venv/bin/voxd`.

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

Tests: `.venv/bin/python -m pytest tests/` — **181 passing** (147 before this
work).

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

- The old Codex output directory (`~/Documents/Codex/2026-07-10/.../vox-mcp`)
  is superseded and can be deleted.
