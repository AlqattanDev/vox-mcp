# Feature map

| VoiceMode capability | Vox implementation |
|---|---|
| Speak + listen / speak-only / listen-only | Native MCP tools |
| Voice, speed, language, prompt, min/max listen | Native per turn |
| Silence detection and pre-roll | Native-rate adaptive recorder |
| Repeat and wait phrases | Native conservative spoken intents |
| Manual recovery from latest audio | Native bounded recovery store |
| Whisper and Kokoro health/failover | Native bounded supervisor |
| macOS offline fallbacks | Native `whisper-cli` and `say` |
| Voice registry/personas | Native resources plus compatibility data |
| Multi-turn surveys / partial results | Native `voice_survey` tool |
| Pause/resume/stop/replay/manual end | Native control channel |
| Multi-agent single-speaker coordination | Native daemon lease/handoff |
| Service lifecycle and logs | Native, non-mutating-at-start supervisor |
| Audio devices and diagnostics | Native diagnostics |
| Whisper model utilities/benchmark | Frozen compatibility CLI; active runtime stays pinned |
| MLX/Qwen voice impressions | Native clone routing to loopback `:8890`; optional local model service |
| Exchange/statistics tooling | Native redacted metrics + compatibility reader |
| Soundfonts and Claude hooks | Frozen compatibility layer |
| DJ/library/Music for Programming | Native DJ/library actions plus frozen compatibility CLI; remote playback disabled |
| Pronunciation rules | Native TTS/STT preprocessing using local rules |
| CLI and completions | Native CLI; compatibility commands retained |
| HTTP MCP serve/bridge | Native persistent loopback HTTP MCP |
| Remote Connect/cloud relay | Disabled by local-only policy |
| Tailscale/LAN exposure | Deliberately removed; Vox accepts literal loopback only |
| Transcript echo convention | Shared Vox skill for Claude Code and Codex |

Vox also adds capabilities the installed version does not provide reliably:

- A real persistent session with deterministic close/pause semantics.
- One capture stream per *turn* behind a software gate, shared by everything
  inside that turn, so a Bluetooth headset is not re-opened twice per turn —
  which is what made turns start on the stream-open transient rather than on
  anyone speaking. Frames are dropped in the realtime callback whenever the gate
  is shut, and the device is released as soon as nothing needs it, so the macOS
  microphone indicator means what it says.
- A 5-second speech-onset timeout instead of leaving the mic open for 90s —
  waived for a turn the user opened with the key, which has said outright that
  they intend to speak.
- Thread-safe cancellation that waits for confirmed microphone closure.
- A direct MCP `listen` tool, push-to-talk/manual end, and host-independent
  replay/control.
- Permission-free global hotkeys on a single two-key combo: **⌘§** tapped is a
  turn key, **⌘§** held is system-wide dictation at the cursor, and **⇧⌘§**
  reads the selection aloud. The last two need no agent at all and keep working
  with every MCP host closed.
- A floating pill that shows, on screen, whether Vox is warming up, listening,
  dictating, or speaking — and is absent the rest of the time.
- A visible native macOS status/permission launcher.
- Frozen, side-by-side, health-checked installs with rollback.
- Loopback-only backend bindings and bounded private storage.

Remote Connect, cloud speech providers, and LAN exposure are intentional
non-parity items because they violate Vox's local-only boundary. Voice-clone
CRUD works immediately; speaking a clone additionally requires a local
MLX/Qwen service on port 8890, whose readiness is shown by the voice registry.
