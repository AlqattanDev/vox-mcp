# Vox MCP

Vox is a local-only voice runtime for Claude Code, Codex, and any MCP client.
It keeps the audio engine in a persistent daemon and makes each MCP server a
small reconnectable adapter. Restarting or updating an agent host therefore
does not tear down the voice engine or corrupt its session state.

Vox uses the Whisper.cpp and Kokoro services already installed on this Mac.
It makes **no paid or cloud speech calls**. Speech endpoints must resolve to a
loopback address; Vox refuses to start if they do not.

## What is different

- Explicit `OFF → IDLE → LISTENING / SPEAKING → IDLE` lifecycle instead of a
  hidden state machine inside an MCP subprocess.
- Competing audio turns queue in FIFO arrival order instead of failing, so a
  second agent — or one agent's own next `converse` — waits its turn rather
  than losing the reply. Waiting is bounded at 30s, and `cancel`/`stop` drain
  the queue instead of orphaning it.
- The microphone opens only for one bounded `listen` turn or a
  response-waiting `converse`; manual end provides push-to-talk behavior.
- `pause`, `resume`, `stop`, replay, manual end-of-turn, and cancellation are
  daemon controls and remain available even when an MCP adapter reconnects.
- Native microphone sample-rate capture with a single high-quality resample;
  no hard-coded 24 kHz device assumption.
- Watchdog health probes and bounded restart/retry for local Whisper/Kokoro.
- A click-to-open macOS status panel with explicit start, pause/resume,
  cancel, stop, restart, and local activity controls. It states whether the
  microphone is actually open and explains an automatic idle stop.
- Frozen dependency lock and direct venv launcher. No `uvx --refresh`, no code
  download, and no version drift at MCP startup.
- Last-audio recovery is on by default; conversation archives and raw text
  logging are off by default.

## Status

This directory contains the implementation, installer, host skills, tests,
and the frozen VoiceMode compatibility layer. See `docs/EXPERIENCE.md` for the
listening contract and `docs/ARCHITECTURE.md` for the process model.

Build, inspect the dry-run, then activate transactionally:

```bash
uv sync --extra test --frozen
./scripts/build_macos_app.sh
uv run vox install
uv run vox install --activate --yes
```

Quick checks after installation:

```bash
.venv/bin/vox doctor
.venv/bin/vox session start
.venv/bin/vox status
```

The MCP tools are headed by `converse`, `speak`, `listen`, `voice_session`,
`voice_control`, `service`, `diagnostics`, `transcribe`, and `dj`.

## Multiple agents, one microphone

Every Claude Code window reports the same MCP host name, so the lease cannot
tell two projects apart — and it deliberately still does not, because keying
the lease to the host is what lets it survive a reconnect. Instead each
project identifies its *speaker* in its own MCP URL:

```json
{ "mcpServers": { "vox": { "url": "http://127.0.0.1:8766/mcp?agent=infra" } } }
```

Set it once per project and no agent can forget it. Callers that cannot set a
URL may pass `agent="..."` to `speak`, `converse`, `listen`, or
`voice_session`; the URL wins if both are present, and an unset agent is
`default`.

Each agent gets its own voice, because when a queued agent finally speaks the
voice is how you know which project is talking. Assignments live in
`~/.vox/agents.json`:

```json
{ "infra": "am_michael", "mobilescape": "af_bella" }
```

Edit it to pick voices yourself — a hand-written entry is honoured exactly,
including a non-English voice. Anything unmapped is assigned automatically
from the local English Kokoro voices, keyed off a hash of the label so it
never drifts between restarts, and written back to the file. The `default`
agent keeps the standard voice, so single-agent use is unchanged.

`voice_session(action="status")` reports the calling agent's resolved voice,
the active turn's agent, and the current queue; `voice_registry` lists the
whole mapping. Queue transitions are logged to `~/.vox/state/events.jsonl`.

Restart Claude Code and Codex once after activation so their MCP catalogs
reload. The Vox daemon persists across later host restarts and updates.

The `Vox` item in the macOS menu bar is a local control panel, not an
always-listening toggle. Click it to start/pause/stop a session or cancel the
current turn; starting or resuming only makes Vox ready. The microphone stays
closed until a host calls `listen` or response-waiting `converse`.
