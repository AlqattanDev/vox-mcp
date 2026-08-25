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
- One capture stream per turn with a software gate in front of it, shared by
  everything inside that turn rather than opened and closed twice within it.
  Audio reaches the endpointer and Whisper only while the gate is open — which
  is only while a turn is actually running. With the gate shut, frames are
  dropped in the realtime callback: nothing is queued, buffered, or classified.
  The device itself is released once nothing needs it, so macOS's own
  microphone indicator is lit only when Vox can genuinely hear you.
- Global hotkeys that work in any app and need no permission grant, because
  Carbon hotkeys need none: **⌘§** held dictates at the cursor, **⌘§** tapped
  reads the selection aloud, and **⌘⇧§** — while an agent is talking — flips
  **the floor**, meaning who speaks next: it claims the microphone for the
  moment that utterance ends, or waves off one you were about to be offered. A tap never opens the
  microphone; listening happens only while the key is held.
- A floating pill at the bottom of the screen that appears only while Vox is
  warming up, listening, dictating, or speaking, with a live waveform of your
  actual microphone level.
- `pause`, `resume`, `stop`, replay, manual end-of-turn, and cancellation are
  daemon controls and remain available even when an MCP adapter reconnects.
- Native microphone sample-rate capture with a single high-quality resample;
  no hard-coded 24 kHz device assumption.
- Watchdog health probes and bounded restart/retry for local Whisper/Kokoro.
- A click-to-open macOS status panel with explicit start, pause/resume,
  **I'm done talking** (close and transcribe the current recording), cancel,
  stop, restart, and local activity controls. It states whether the
  microphone is actually open and explains an automatic idle stop.
- Frozen dependency lock and direct venv launcher. No `uvx --refresh`, no code
  download, and no version drift at MCP startup.
- Last-audio recovery is on by default; conversation archives and raw text
  logging are off by default.

## Status

This directory contains the implementation, installer, host skills, and
tests. See `docs/EXPERIENCE.md` for the listening contract and
`docs/ARCHITECTURE.md` for the process model.

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
`voice_control`, `service`, `diagnostics`, and `transcribe`.

## Talking to it from a phone

The microphone and the speaker do not have to be the ones attached to this
machine. Expose the two phone paths over Tailscale once:

```bash
tailscale serve --bg --set-path=/phone    http://127.0.0.1:8766/phone
tailscale serve --bg --set-path=/phone/ws http://127.0.0.1:8766/phone/ws
cat ~/.vox/control.token
```

Then open `https://<this-machine>.<tailnet>.ts.net/phone` on the phone, paste
that token, and tap Connect. The browser captures at 16 kHz and plays what it
is sent; Whisper and Kokoro still run here, so the audio reaches nothing but
the phone in your hand. Every tool is unchanged — `converse`, `listen`,
`speak`, the earcons and barge-in all follow the phone, because none of them
ever addressed hardware directly.

`/phone` and `/phone/ws` are the only surfaces reachable from off this machine.
They require a tailnet or loopback peer, the socket requires the control token,
and `/mcp` and `/control` remain loopback-only.

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
current turn; starting or resuming only makes Vox ready. A session holds one
capture stream open, but the gate in front of it stays shut until a turn
actually runs, so nothing you say between turns reaches the endpointer or
Whisper. `pause`, `mute`, and `stop` release the device outright.

## Working without an agent

Two of the hotkeys have nothing to do with MCP and keep working with every
agent host closed:

- **⌘§ held** dictates into whatever app is focused. Everything between press
  and release goes to Whisper — no endpointing, no voice activity detection,
  because the key already says where the utterance starts and stops. Local rules
  strip Whisper's ambient annotations and spoken hesitation
  (`VOX_DICTATION_CLEANUP=rules|off`); no model rewrites what you said.
- **⌘§ tapped** reads the current selection aloud, word for word. The text goes
  straight to Kokoro, so there is nothing on the path that could paraphrase a
  number, a name, or a line of code. Tap again to stop. It queues behind an
  agent that is already speaking rather than talking over it. It never opens the
  microphone.

**Dictated text is left on your clipboard** after it is pasted at the cursor. A
paste into a surface with no editable field silently goes nowhere; leaving the
words there means the fallback is always ⌘V. Only the keybinding does this —
what you say in conversation with an agent is not copied, because it is not
going anywhere you would have to paste it (`VOX_CLIPBOARD_TRANSCRIPT=1` puts
spoken transcripts on the clipboard too).

## Permissions

- **Microphone** — required for everything. Requested on first launch.
- **Accessibility** — required only for the ⌘§ gestures, which post ⌘V/⌘C and
  read the focused element. Vox prompts the first time you use either, and
  says so in the panel rather than failing silently. Grant it to
  `Vox` under System Settings › Privacy & Security › Accessibility. ⌘⇧§ needs
  none of it: it flips a flag in the runtime and posts no keystrokes.

macOS pins these grants to the app's code signature, so the build refuses to
fall back to ad-hoc signing: an ad-hoc signature changes on every build and
would revoke Accessibility on every install with no error to explain why
dictation stopped working. Set `VOX_CODESIGN_IDENTITY` if the identity is not
found automatically.
