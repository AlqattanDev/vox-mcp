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
- **Observability.** `voice_session(status)` reports the active agent, the
  queue, and the caller's resolved voice; `voice_registry` lists the mapping.
  Queue transitions land in `~/.vox/state/events.jsonl`.

Tests: `.venv/bin/python -m pytest tests/` — **179 passing** (147 before this
work).

## Known issue, not yet fixed

Cancelling a turn **while it is speaking** leaves that caller with no response:
the engine cancels the handler task, so the MCP tool call never completes and
the calling agent hangs. Queued turns are unaffected — they drain correctly.

This predates the queue work (verified by running the same scenario against the
pre-change commit, which behaves identically). Fixing it means changing cancel
semantics to return a `cancelled` result instead of propagating
`CancelledError`, which is a deliberate design change rather than a bug fix.

## Next steps

- Decide on the cancel-while-speaking semantics above.
- Add `?agent=<project>` to each project's MCP config; anything unset stays
  `default` and sounds exactly as before.
- The old Codex output directory (`~/Documents/Codex/2026-07-10/.../vox-mcp`)
  is superseded and can be deleted.
