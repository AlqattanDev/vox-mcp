# Vox voice suite — verified implementation spec

Verified 2026-07-27 against the `vox-mcp` source (package `src/voxmcp/`, the
live runtime), the raw event log (`~/.vox/state/events.jsonl`, session
2026-07-27 01:14–01:56 — the session that produced VOICE-GATE-BRIEF.md), the
system TCC database, and **live reproduction** of the mic-flicker failure on
the actual hardware (HUAWEI FreeClip 2, connected, default input at 16 kHz
HFP). Every UNVERIFIED claim in VOICE-GATE-BRIEF.md is resolved below. Build
from this document; the brief is superseded.

Three features:

1. **Persistent mic + keyboard gate** — open the capture stream once per
   session; a software gate decides whether audio reaches endpointing/STT; a
   global hotkey opens the gate + starts the turn and closes the gate + ends
   **and submits** the turn.
2. **System-wide hold-to-talk dictation** (Wispr Flow replacement) — hold key
   → capture → local Whisper → text injected at the cursor of whatever app is
   focused. One-way, no agent, no TTS, MCP-independent.
3. **Read selected text aloud, verbatim** — key → capture OS selection →
   Kokoro TTS exactly as written. No LLM anywhere in the path,
   MCP-independent.

---

## 1. Verdicts on every claim marked UNVERIFIED in the brief

| # | Brief's claim | Verdict |
|---|---|---|
| 1 | Mic is opened and closed **twice per conversational turn** (barge-in capture during TTS, teardown at `tts.completed`, fresh listen ~130 ms later) | **CONFIRMED** — in code and in the log. `_arm_barge_in` (engine.py:681) opens a real `InputStream` via `AudioRecorder.capture()` in a worker thread; `_disarm_barge_in` (engine.py:725) cancels it after playback when no barge-in fired; `_capture_once` (engine.py:1122) then opens a **second, brand-new** `InputStream`. Measured gap `tts.completed → listening.started`: 126–144 ms across 12 turns (incident turn: 138 ms). When barge-in fires, one capture serves both (no second open) — also confirmed. |
| 2 | Mic flicker: window opened ~139 ms after TTS, closed at 0.72 s on `trailing_silence` with ~0.08 s "speech", `stt.non_speech`, twice | **CONFIRMED, mechanism identified and REPRODUCED** (§3). Cause: a **Bluetooth HFP stream-open transient**, not echo, not room noise. |
| 3 | Echo tail of Vox's TTS caused the false onsets | **REFUTED** — reproduced the identical false-open from a cold start with **no TTS played at all** (twice in a row). Also, the start earcon plays *blocking, to completion, before* the stream opens (engine.py:1063-1074), so no playback overlaps capture on the non-barge-in path. |
| 4 | Ali's speech fell in a dead gap between two closed-mic windows | **CONFIRMED as the reason the words were lost** (but the windows themselves were transient false-opens, not caused by his speech placement). Measured: window 1 closed 01:40:48.4, window 2 opened 01:40:53.2 — **4.8 s of closed mic between them** — and after window 2 closed at 01:40:56.5 the mic stayed **closed for 113 s** while the agent composed. Anything said in those spans reached nobody. |
| 5 | Bluetooth transient/pop registering as a ~0.1 s onset | **CONFIRMED — this is the actual mechanism.** See §3 for the waveform evidence. |
| 6 | Bluetooth profile switching is not the seam | Correct as stated for the *per-turn seam* (that's the stream teardown), but the HFP **input engagement transient is** what falsely opens turns. The two are different failure modes; both are real. |
| 7 | `voice_session` mute/unmute might already be the gate | **REFUTED — mute is not a gate.** `mute` is a pure alias for `pause` (mcp_server.py:478; engine.py:1700): it **cancels** the in-flight capture and playback, **drains the queue**, **blocks until the mic stream is closed** (fail-closed), and pauses the state machine. `unmute` = `resume`, which only transitions PAUSED→IDLE and never reopens anything (state.py:241). This is a privacy teardown, the exact opposite of "keep the stream and gate it." Keep it as-is; the gate is a new, separate concept (§4). |
| 8 | Whether the capture loop fits a "sink" abstraction or needs a big refactor | **Fits cleanly.** `AudioRecorder.capture_from_frames(frames, sample_rate, control=…, on_speech_started=…)` (audio.py:913) already drives the *complete* endpointing + recovery path from any frame iterable with no device attached. A persistent stream pump that fans frames out to consumers slots straight in; only the two `capture()` call sites change (engine.py:1139 and :704). |
| 9 | Vox already holds macOS Accessibility permission | **REFUTED — it does not.** System TCC db has **no entry** for `local.vox.mcp.status` (Vox.app's bundle ID) under `kTCCServiceAccessibility`, `kTCCServiceListenEvent`, or `kTCCServicePostEvent`. The only permission Vox holds is Microphone (`NSMicrophoneUsageDescription` declared; capture demonstrably works daily). A grant flow is required for F2/F3 (§7). Notably `com.electron.wispr-flow` *is* in the Accessibility list — Wispr Flow itself uses exactly this permission for injection. |
| 10 | Kokoro playback can be invoked without an agent turn | **CONFIRMED with a caveat.** Internally trivial: `_render_tts` (engine.py:871) → `LocalSpeechClient.synthesize` (speech.py:155, a loopback POST to Kokoro :8880) → `AudioPlayer.play_file` (afplay subprocess). Earcons already play through `AudioPlayer` with no agent. **But no external trigger exists today**: the `/control` allowlist (mcp_server.py:861-877) has no speak-like action; `vox speak` goes through the MCP tool surface (the CLI is an MCP client). F3 adds a `/control` action (§6). |
| 11 | MacBook built-in mic endpointing is poor ("kept listening forever") | **NOT RE-TESTED** (would require changing the system default input). User-reported; treat as true operationally. Note it predates the adaptive noise floor (STATUS.md documents 7.24 s → 1.6 s on one word after that landed), so it may be stale — irrelevant either way, since key-press endpointing removes the dependency. |
| 12 | FreeClip 2 = 16 kHz HFP input | **CONFIRMED** — `system_profiler`: input 16 000 Hz, and the reproduction captures came back `sample_rate: 16000`. Output side idles at 44.1 kHz A2DP; opening any capture drags it into the HFP call profile (documented cost, STATUS.md). |
| 13 | Wispr Flow product facts (Fn hold, cloud STT, Llama cleanup, ~700 ms p99) | Not re-verified (external product). Immaterial to implementation. One local corroboration: Wispr Flow is installed on this Mac with Accessibility granted. |

---

## 2. Verified system map (what exists today)

**Processes.** launchd agent `com.vox.runtime` (`~/Library/LaunchAgents/com.vox.runtime.plist`)
runs `~/Applications/Vox.app/Contents/MacOS/VoxStatus` — the Swift menu-bar
app (single 907-line file `macos/VoxStatus.swift`, `LSUIElement`, activation
policy `.accessory`). VoxStatus **spawns the Python runtime as its child**:
`$VOX_RUNTIME` = `~/vox-mcp/.venv/bin/voxd` (VoxStatus.swift:352-383, with
`VOX_PARENT_PID` + a ppid watchdog in daemon.py:73). Both are running now
(pids observed). There is **one** app bundle; "VoxStatus" is only the
executable name inside Vox.app.

**Bundle/signing.** `Vox.app`, bundle ID `local.vox.mcp.status`, currently
signed with an Apple Development identity, **TeamIdentifier YN9839UZF5**.
Build script (`scripts/build_macos_app.sh:22-31`) falls back to **ad-hoc**
signing if no identity is found — see the TCC warning in §7.

**Runtime surface** (all loopback-only, port 8766):
- `/mcp` — Streamable HTTP MCP (FastMCP), stateful.
- `/health` — GET, no auth beyond loopback. Fields include `state`,
  `microphone_open`, `mic_armed_for_barge_in`, `mic_level`, `io_mode`,
  `agents`, `notes_waiting`, `last_spoken_agent` (engine.py:2316-2343).
- `/control` — POST, token-authed (`~/.vox/control.token`, sent by VoxStatus
  as `X-Vox-Token`; bearer also accepted). Action allowlist
  (mcp_server.py:861-877): `start cancel end_turn stop pause resume mute
  unmute status cycle_mode set_mode repeat note reply deliver_text`.
  `note`/`reply` return 202 fire-and-forget.

**The runtime runs with zero MCP clients connected.** voxd boots the HTTP
listener + engine object only; nothing about `/control` or `/health` requires
an MCP session. Whisper (`com.vox.whisper`, :2022) and Kokoro
(`com.vox.kokoro`, :8880) are separate always-on launchd jobs. This is why F2
and F3 fit at the runtime level with no MCP dependency.

**Hotkey precedent.** ⌃⌥⌘R is already a **global, permission-free** hotkey:
Carbon `RegisterEventHotKey(kVK_ANSI_R, controlKey|optionKey|cmdKey, …)` in
VoxStatus.swift:146-177 — explicitly chosen over an NSEvent global monitor
because Carbon needs **no Input Monitoring or Accessibility grant**. Carbon
delivers both `kEventHotKeyPressed` and `kEventHotKeyReleased`, so
**hold-to-talk (press/release) works permission-free too**. Limitation: a bare
**Fn** key (Wispr's default) is *not* registrable via Carbon; it would need a
CGEventTap + Input Monitoring. Use Carbon combos; do not chase Fn.

**Capture path.** One blocking call per listen: `AudioRecorder.capture()`
(audio.py:1009) resolves the default input at its **native rate**, opens a
`sounddevice.InputStream` inside a `with` block, pumps 20 ms frames through
`AdaptiveCaptureState.feed()`, and **closes the stream when the capture
ends**. There is no persistent stream anywhere today. Key defaults:
`speech_start_s = 0.06` (60 ms of speech-classified frames = onset),
`pre_roll_s = 0.3`, `min_duration_s = 0.5`, short/long trailing silence
0.6 s/1.6 s scaled over measured speech 1.5–3.0 s, `onset_timeout_s = 15`.
The barge-in armed capture uses a hardened config (`armed_capture_config`,
engine.py:1085): `speech_start_s = 0.30` (via `VOX_BARGE_IN_SPEECH_START_SECONDS`),
18 dB margin, 0.8 s noise window, **no onset timeout**.

**Manual end already exists.** `POST /control {"action":"end_turn"}` →
`engine.control` `manual_end`/`push_to_talk_end` (engine.py:1790) →
`CaptureControl.end_utterance()` → the capture stops with reason
`manual_end` and **submits normally**. The menu-bar glyph's left-click uses
it today; the incident session shows `reason: manual_end` turns working.
This is the "second tap ends and submits" primitive — F1 reuses it verbatim.

**Playback.** Always an `afplay` subprocess (`AudioPlayer`, audio.py:1250);
stop = SIGTERM→SIGKILL on the handle (`PlaybackHandle.cancel`). No AEC exists
or is possible (no reference signal from an opaque subprocess). Streaming TTS
speaks sentence-by-sentence (`split_for_tts`, engine.py:63) and aborts
remaining spans on cancel/barge-in.

---

## 3. The mic-flicker: verified mechanism, with evidence

**Reproduced on demand, twice, 2026-07-27 02:04, FreeClip 2 connected, no TTS
playing, nobody speaking.** Both bare `listen` calls false-opened and closed:

```
02:04:37.206 listening.started
02:04:41.036 listening.stopped  duration 0.96s  reason trailing_silence  speech=true
02:04:41.461 stt.non_speech    (whisper.cpp, 422ms)
02:04:58.461 listening.started
02:05:01.693 listening.stopped  duration 0.94s  reason trailing_silence  speech=true
02:05:01.913 stt.non_speech    (whisper.cpp, 218ms)
```

Identical signature to the incident (0.72 s window / `speech: true` /
`stt.non_speech`). The recovery WAV of the reproduction
(`~/.vox/audio/latest/stt.wav`, 16 kHz) shows exactly what the mic delivered:

- **0–230 ms: pure digital silence** (−96 dBFS) — the HFP input pipeline
  warming up, delivering zero samples;
- **~240 ms: a broadband burst at −27 dBFS** decaying smoothly to −60 dBFS
  over ~350 ms — a stream-open artifact of the Bluetooth HFP input engaging.
  Nothing acoustic happened in the room.

That burst is 24 dB above the initial threshold (initial floor −60 + 9 dB
margin = −51 dBFS), WebRTC VAD endorses broadband noise, and 60 ms of it
satisfies `speech_start_s = 0.06` → onset fires → burst decays → 0.6 s short
trailing silence → close at ~0.9 s → Whisper sees a pop and returns
non-speech. Every number in the incident log falls out of this arithmetic.

**Why only some turns visibly flickered:** the transient fires at *every*
fresh stream open on this device, but when Ali starts talking promptly his
real speech merges into the capture and hides it. The two "flicker" windows
are simply the two openings where he wasn't already talking. The armed
barge-in capture never false-fires because its config requires **0.30 s of
sustained onset** — which the ~350 ms decaying burst fails (it drops below
the armed margins long before 300 ms of consecutive speech-frames accrue).

**Consequences for the design (all three fixes in the brief remain right,
now with confirmed reasons):**
1. The persistent stream removes per-turn opens, so the transient can fire at
   most **once per session** instead of once per turn.
2. The gate boots **closed**, so the one remaining open-transient is discarded
   before any endpointer sees it. Belt-and-braces: the pump should also drop
   the first ~500 ms after any (re)open (§4, `stream_open_guard_s`) — note
   the burst arrives ~240 ms *after* open, so a guard shorter than ~500 ms is
   not safe; the brief's suggested 250–400 ms post-TTS guard would have been
   too short *and* aimed at the wrong boundary (the trigger is stream-open,
   not tts.completed).
3. Key-press turn boundaries mean a transient can neither start nor submit a
   turn even if one slips through.

---

## 4. Feature 1 — persistent capture + gate + keyboard turn control

### 4.1 Architecture

New module `src/voxmcp/capture_source.py`:

```
PersistentCaptureSource
  ├─ owns ONE sounddevice.InputStream (native rate, 20ms frames,
  │   same callback shape as AudioRecorder.capture: mono float32 via
  │   _as_mono_float32, bounded queue, drop-oldest)
  ├─ open(): resolve_input_device() → open stream → discard all frames for
  │   stream_open_guard_s (default 0.5s; env VOX_STREAM_OPEN_GUARD_SECONDS)
  ├─ gate: threading.Event, default CLOSED
  │   ├─ closed → frames dropped at the pump; nothing buffered, nothing
  │   │   classified; publish mic_level=0 (deaf means deaf — background
  │   │   audio never reaches STT)
  │   └─ open  → frames fan out to the current subscriber + a rolling
  │       pre_roll_s ring buffer (so a turn that attaches late still gets
  │       its opening words — same trick barge-in's pre-roll uses)
  ├─ subscribe() → iterator of frames (one active consumer at a time is
  │   enough; turns are serialized by the existing operation gate)
  └─ close(): session end / pause / stop → stream torn down (privacy
      semantics unchanged)
```

Turns then run through the **existing, tested** device-free path:
`AudioRecorder.capture_from_frames(source.subscribe(), source.sample_rate,
control=…, on_speech_started=…)` — full endpointing, recovery WAV, pre-roll,
CaptureControl semantics, zero new state-machine code.

Touch points:
- engine.py:1137-1139 (`_capture_once`): use the source when the session holds
  one, else fall back to `capture()` (keeps `vox` usable with no session
  stream, and keeps tests green).
- engine.py:702-709 (`_arm_barge_in`): same substitution; arming becomes
  attaching an armed-config endpointer to the live stream — no second device
  open, which also ends the audible HFP volume-collapse at disarm.
- Stream lifecycle: open on `session.start` / first gate-open; close in
  `session(action="pause"|"mute"|"stop")` right where `_signal_cancel` +
  `_wait_for_microphone_closed` already run (engine.py:1700-1728). **Pause
  keeps its current meaning** (full teardown, fail-closed privacy hold); the
  gate is the new lighter control layered above it. Do not merge them —
  verified `mute` semantics (§1 row 7) are a hard privacy stop and callers
  rely on that.
- Instrument the source: log `capture.stream_opened` / `capture.stream_closed`
  events. Acceptance below keys on them.

`microphone_open`/`mic_level` on `/health` must now derive from
**gate-open** (audio can reach STT), not stream-open — the red glyph should
mean "Vox can hear you," which with a persistent stream is the gate, not the
device. Add `gate_open: bool` to `/health` and the status payload; keep
`microphone_open` as the user-meaningful "can hear" flag so the glyph and
panel stay honest without Swift changes beyond the new button/label.

### 4.2 Keyboard control (the turn boundary)

- **Tap-to-toggle, not hold** (40–60 s turns; hold is F2's interaction, not
  F1's). Second Carbon hotkey in VoxStatus alongside ⌃⌥⌘R
  (VoxStatus.swift:146-177 is the template; new `EventHotKeyID` id 2).
  Default suggestion **⌃⌥⌘L** (`kVK_ANSI_L`); Ali is open on the combo. Must
  be a Carbon-registrable combo — bare Fn is impossible without Input
  Monitoring (§2).
- Tap 1 → `POST /control {"action":"gate_open"}`:
  - gate opens; if no listen is in flight, the runtime starts one (the
    note/reply path at engine.py:1550-1593 is the model: 202, fire-and-forget
    capture addressed to `last_spoken_agent`); if a listen *is* pending
    (converse waiting on the mic), it simply starts receiving frames.
  - While the gate is open, run the listen with **no onset timeout**
    (onset_timeout_s=None, exactly like the armed capture) — the key said
    "I'm talking"; the runtime must not hang up first.
- Tap 2 → `POST /control {"action":"gate_close"}`:
  - calls the **existing** manual-end path (engine.py:1790 →
    `control.end_utterance()` → reason `manual_end` → transcribe → submit),
    then closes the gate.
- Trailing-silence endpointing **stays on as a fallback** during gate-open
  (unchanged config); the key is simply the primary end signal. This
  satisfies "works on any input device" without deleting working behavior.
- VoxStatus wiring is mechanical: `sendControl("gate_open"/"gate_close")`
  next to the existing `end_turn`/`reply` senders (VoxStatus.swift:688-728);
  both new actions join the `/control` allowlist (mcp_server.py:861-877).

### 4.3 Feedback

Reuse what exists — all three channels are already built:
- **Earcons**: rising/falling blips already mark mic open/close
  (earcons.py, `_play_listen_start_cue` / `_play_listen_stop_cue`); fire them
  on gate flips instead of stream opens.
- **Menu-bar glyph**: red-mic-when-hot already keyed to `microphone_open`;
  with the /health change above it tracks the gate for free.
- **Panel**: hero state word + `mic_level` waveform already poll `/health` at
  0.08 s while open.

### 4.4 Acceptance (against the verified failures)

1. One `capture.stream_opened` per session on the FreeClip; **zero** between
   turns while the gate stays open (the old churn was 2 opens/turn).
2. With the gate closed, play YouTube/music for minutes: zero
   `listening.started`, zero `stt.*` events.
3. Cold gate-open with nobody speaking must **not** reproduce §3's false
   window (the open-guard + single session open kill the transient); assert
   no `listening.stopped` with `duration_s < 1.5` + `stt.non_speech`.
4. Tap-open → talk 60 s with long pauses → tap-close: exactly one turn,
   transcript complete, no mid-thought endpoint.
5. Barge-in on earbuds still works gate-open (armed attach, no second
   stream), and never arms gate-closed.
6. `pause`/`resume`, `stop`, `deliver_text`, panel buttons, ⌃⌥⌘R unchanged.

---

## 5. Feature 2 — system-wide hold-to-talk dictation

### 5.1 Interaction & data path (verified fit)

```
hold key (Carbon pressed)             VoxStatus
  → POST /control {"action":"dictate_start"}
      runtime: capture raw frames (no VAD, no endpointing — press-to-release
      IS the utterance; max_duration backstop 120s)
release key (Carbon released)
  → POST /control {"action":"dictate_end"}
      runtime: stop capture → LocalSpeechClient.transcribe(wav)  [Whisper :2022]
      → optional local cleanup → response body carries {"text": …}
      VoxStatus: inject text at cursor (§5.2)
```

- Carbon delivers `kEventHotKeyPressed` **and** `kEventHotKeyReleased` — hold
  works with the existing permission-free mechanism (§2). Suggested default
  **⌃⌥⌘D**; Ali open on the binding.
- **No VAD anywhere**: the transient of §3 and endpointing quality are both
  irrelevant — everything between press and release goes to Whisper, which
  shrugs off a 350 ms pop. (Verified behavior: Whisper returned clean
  non-speech for the pure transient; with real speech present it transcribes
  through noise.) Dictation may open its own short-lived stream OR reuse the
  persistent source when a session holds one open — either is correct; do
  not couple F2 to F1's session lifecycle (F2 must work with **no** session
  and **no** MCP client, which `/control` already permits, §2).
- Engine addition: `dictate_start`/`dictate_end` actions + a capture mode
  that bypasses `AdaptiveCaptureState` (collect frames → `write_wav_atomic` →
  `speech.transcribe`). It should hold the operation gate so it serializes
  against agent turns, but must **not** require `session.start` (follow the
  `administrative`/`http-control` client pattern, engine.py:1650).
- VoxStatus `/control` timeout is 6 s (VoxStatus.swift:698) — Whisper
  measured 218–2100 ms in this session's log, so a synchronous
  `dictate_end` response fits; bump the timeout for this one call to be safe
  with `small`-model worst cases on long holds.
- **State to respect:** if TTS is playing or a listen is live, dictate wins —
  it's an explicit user act; cancel current playback (existing
  `_signal_cancel`) before capturing.

### 5.2 Text injection — decided method

**Default: clipboard + synthesized ⌘V, implemented in VoxStatus (Swift).**

- Sequence: snapshot `NSPasteboard.general` (all types) → `clearContents` +
  `setString(text)` → `CGEvent(keyboardEventSource:)` ⌘V down/up posted to
  `.cghidEventTap` → after ~300 ms, restore the snapshot.
- Why clipboard-paste over the alternatives (brief's list, adjudicated):
  - `CGEventCreateKeyboardEvent` per-character typing: slow for 40–60 s
    dictations (hundreds of chars), breaks on non-ASCII/Arabic — Ali
    dictates Arabic; disqualifying.
  - `AXUIElement` value insertion: cleanest when it works, but write-access
    to `AXValue`/`AXSelectedText` is unimplemented or read-only in Chromium,
    Electron, and many custom views — the exact apps Ali lives in. Fine as a
    later enhancement, wrong as the default.
  - Clipboard+⌘V is what SuperWhisper-class local tools ship; it works in
    every app that has a Paste command, including secure text fields'
    nearest equivalent behavior.
- **Injection lives in Swift, not Python.** The CGEvent post requires
  Accessibility, and the TCC grant attaches to the responsible app bundle —
  VoxStatus *is* the app (`local.vox.mcp.status`) and already owns the
  hotkeys. Granting the Python child separately is fragile and unnecessary;
  keep the runtime permission-free. (Permission flow: §7.)
- Paste target = whatever app is focused; VoxStatus is `LSUIElement`/accessory
  so the hotkey never steals focus. No focus juggling needed.

### 5.3 Cleanup pass

Start **rule-based, on by default, in the runtime** (so any future consumer
gets it): trim whitespace, strip leading/trailing Whisper artifacts
(`[BLANK_AUDIO]`-class markers — `is_non_speech_transcript` already knows
them), collapse fillers ("uh", "um", Arabic equivalents) via a small regex
list, capitalize first letter, ensure terminal punctuation. Whisper already
emits punctuation/casing, so this is polish, not reconstruction — and it adds
zero perceptible latency. A local-LLM pass is a later, separate knob
(`VOX_DICTATION_CLEANUP=rules|off|llm`), default `rules`; **never cloud**
(config.local_only stays unqualified — the companion precedent applies).

### 5.4 Acceptance

1. With **no MCP client running** (quit Claude Code), hold-dictate into
   Chrome (address bar + a Google Doc), Notes, Slack/Discord, and a terminal;
   text lands at the cursor, clipboard contents restored afterward
   (verify with an image on the clipboard, not just text).
2. Arabic dictation round-trips (UTF-8 through pasteboard is trivially safe;
   this is why keystroke-synthesis lost).
3. Release-to-text-visible latency: target < 1.5 s for a 10 s utterance
   (Whisper `small` measured 218 ms–2.1 s in-log; budget is realistic).
4. No TTS, no agent turn, no `tts.*` events during any dictation.

---

## 6. Feature 3 — read selection aloud, verbatim

### 6.1 Data path

```
key (suggested ⌃⌥⌘S; Ali open)        VoxStatus
  → capture selection (§6.2)
  → POST /control {"action":"read_aloud","text":<selection>}
      runtime: engine.read_aloud → split_for_tts → _render_tts (Kokoro)
      → AudioPlayer, streamed sentence-by-sentence
key again / cancel path
  → POST /control {"action":"cancel"}   (existing; kills afplay via
      PlaybackHandle.cancel — verified mechanism)
```

- **Verbatim guarantee is structural**: `LocalSpeechClient.synthesize`
  (speech.py:155) POSTs the text to Kokoro exactly as given — there is no
  model, prompt, or rewrite anywhere on this path. Do not route through
  `converse`/agents; add a dedicated engine method + `/control` action.
- Reuse `_run_operation` with an administrative client id so read-aloud
  FIFO-queues against agent speech instead of talking over it, but does
  **not** require an MCP session (same pattern as §5.1).
- Long selections: `split_for_tts` streaming already synthesizes span n+1
  while span n plays (engine.py:962) — reuse; first audio ≈ 0.55–0.89 s
  (measured Kokoro first-span latency, STATUS.md).
- Toggle stop: VoxStatus tracks "read-aloud in flight" (it already tracks
  `controlInFlight`); second press sends `cancel`. `/health.state ==
  "speaking"` is the fallback truth source.

### 6.2 Selection capture — decided method

**Try `AXSelectedText` first; fall back to clipboard snapshot + synthesized
⌘C.** Both in VoxStatus (same Accessibility grant as F2, §7).

- AX read: `AXUIElementCopyAttributeValue(systemWide focused element,
  kAXSelectedTextAttribute)` — no clipboard clobber, instant. Known to return
  nothing in some Chromium/Electron surfaces and non-native views — which is
  precisely why it cannot be the only path (and why the brief's "try AX
  first, fall back to ⌘C" is adopted as-is).
- Fallback: snapshot pasteboard → post ⌘C → wait ~150 ms for
  `changeCount` to tick → read string → restore snapshot. If `changeCount`
  never changes (no selection / app blocks copy), speak nothing and play the
  falling earcon as the "nothing to read" cue.
- Empty/whitespace selection → no TTS, error earcon only.

### 6.3 Acceptance

1. No MCP client running: select text in Chrome, Notes, a PDF in Preview, a
   terminal → key → Kokoro reads it **word-for-word** (spot-check against a
   paraphrase-prone sample: numbers, names, code).
2. Second key press stops playback inside ~0.3 s (grace 0.05–0.2 s SIGTERM).
3. Clipboard restored after ⌘C-fallback captures.
4. An agent speaking at the time is not interrupted mid-word by read-aloud;
   read-aloud queues (or, if Ali prefers, cancels it — decide at build time;
   queueing is the default the codebase already gives you).

---

## 7. Shared: hotkey daemon, permissions, signing

**One hotkey owner: VoxStatus.** It already owns Carbon hotkey #1 (⌃⌥⌘R
reply), the control token, `/control` plumbing, and the runtime's lifecycle.
F1/F2/F3 add three registrations to the same `InstallEventHandler` dispatch
(switch on `EventHotKeyID.id`), handling both pressed and released kinds.
Do **not** build a Python-side hotkey listener — the Python runtime has no
key handling today (verified) and needs none.

**Accessibility permission (required for F2 injection + F3 capture, both ⌘V/⌘C
CGEvent posts and AX reads):**
- Current state: **not granted** (verified in TCC, §1 row 9). Microphone is
  the only permission Vox holds.
- Grant flow: on first use of F2/F3, VoxStatus calls
  `AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt: true])` — this
  opens System Settings → Privacy & Security → Accessibility with Vox listed;
  until granted, the feature reports itself unavailable (menu-bar panel line +
  error earcon, never silent failure). Carbon hotkeys themselves stay
  permission-free, so F1 needs no grant at all.
- **Signing caveat (load-bearing):** TCC grants pin to the code signature.
  The build script falls back to **ad-hoc** signing when no identity is found
  (build_macos_app.sh:22-31) — an ad-hoc re-sign changes identity every
  build and the Accessibility grant would silently die on every deploy. The
  machine currently signs with Apple Development team YN9839UZF5; make that
  identity a **hard requirement** of the build once F2/F3 land (fail the
  build rather than fall back), or Ali will be re-granting after every
  `install_macos_app.sh`.
- `NSAppleEventsUsageDescription` is not needed (no AppleScript in the chosen
  paths); no Input Monitoring needed (no event *taps*, only posts + Carbon).

**New `/control` actions summary** (all join the allowlist at
mcp_server.py:861-877, token-authed, loopback-only): `gate_open`,
`gate_close`, `dictate_start`, `dictate_end`, `read_aloud`.

**New env knobs** (all via `~/.vox/settings.json` / `vox set`, since launchd
strips the environment — STATUS.md): `VOX_STREAM_OPEN_GUARD_SECONDS` (0.5),
`VOX_GATE_HOTKEY` / `VOX_DICTATE_HOTKEY` / `VOX_READ_HOTKEY` (combo strings,
parsed in Swift from the health/config handshake or compiled defaults),
`VOX_DICTATION_CLEANUP` (`rules`), `VOX_DICTATION_MAX_SECONDS` (120).

**Order of work:** F1's gate + hotkey plumbing first (it creates the
`/control`-action pattern and the second-hotkey template), F2 next (adds the
Accessibility grant flow + injection), F3 last (smallest delta: selection
read + one engine method). Each lands with its acceptance list run live —
STATUS.md's own warning applies: this codebase has shipped three green-test
bugs that only speaking aloud found. Verify aloud before believing green.
