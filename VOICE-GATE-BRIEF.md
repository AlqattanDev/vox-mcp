# Vox — persistent mic + keyboard-gated listening

> **For the verifying agent (Ali is running this pass on Fable — be efficient,
> Fable time is expensive):** Your job this pass is research + verification, not
> implementation. Read the `vox-mcp` code, reproduce the behaviors, and confirm
> or correct every UNVERIFIED claim below (all three features). When done, write
> a single clean, corrected, implementation-ready spec to
> **`VOICE-SUITE-VERIFIED.md`** in this repo — with the code-level details
> confirmed (file/function touch-points, permission state, chosen injection /
> selection-capture method) — so a cheaper model can implement straight from it
> without re-doing the research. Don't implement here; just verify and write the
> verified spec.

> **VERIFY BEFORE ACTING — do not trust this document's diagnoses as fact.**
> This brief was written from a live voice session by an agent reasoning about
> logs in real time. The *desired design* (sections below) is what Ali wants.
> But every **cause/diagnosis claim** here (echo bleed, transient false-onset,
> timing numbers, why the mic flickered) is a **hypothesis, not confirmed
> truth** — some may be wrong. Before you implement or "fix" anything:
> 1. Reproduce the behavior yourself and read the raw Vox event log
>    (`vox diagnostics`, `section: events`) to confirm what actually happens.
> 2. Confirm each claimed cause against the code in this repo and fresh logs.
> 3. If a claim here is wrong, correct it — don't build on it.
> Ali explicitly asked that these claims be re-verified; treat unverified.

## What we want to build

Kill the per-turn mic seam and give Ali a hardware-feeling on/off control over
when Vox can hear him — without ever tearing the mic stream down.

Two independent changes:

1. **Persistent capture stream.** Open the microphone **once** per voice session
   and keep it open across speaking and listening. Never tear it down at the
   speech→listen boundary.
2. **A software listening gate (mute flag) in front of transcription.** The
   stream is always capturing, but a gate decides whether audio reaches STT:
   - **Gate closed → Vox is deaf.** Audio is discarded before transcription.
     (So background audio — YouTube, calls, music — is never heard or acted on.)
   - **Gate open → normal listen / endpointing.**
   - **Default = closed.** Vox boots deaf and only hears Ali when he opens it.
3. **Toggle = a keyboard shortcut, and the key marks the turn boundary.** Ali
   taps a hotkey to open the gate + start his turn, and taps again to close the
   gate + **end and submit** the turn. **No wake word** — explicitly rejected
   (false triggers). Crucially, the key press — not silence — is the
   end-of-turn signal (see "Endpointing" below).

## Endpointing — the keyboard control replaces VAD (important)

There are two separate jobs: (a) the gate (may Vox hear at all) and (b)
endpointing (knowing when Ali finished a sentence so it can transcribe/respond).
The keyboard control should absorb BOTH so we stop depending on trailing-silence
VAD, which is the flaky thing that failed on the MacBook mic ("kept listening
forever").

- **Make the key press the explicit turn boundary.** Tap to open gate + start
  capturing the utterance; **tap again to close gate + end + submit** the turn.
  The second tap is a deterministic "I'm done" — no silence detection required.
- **Prefer tap-to-start / tap-to-end over hold-to-talk.** Ali's turns run
  40–60 s; holding a key that long is miserable. Toggle-with-explicit-stop gives
  the same deterministic endpointing without the hold.
- **Result:** endpointing no longer depends on mic quality or VAD tuning, so it
  works on any input device — the "listens forever" bug is solved by design, not
  a separate fix. (Trailing-silence auto-submit can stay as an optional fallback,
  but the key press is the primary end signal.)

## Why — the exact problem (from this session's Vox event log)

Per conversational turn today, the mic was opened and closed **twice**:

1. `tts.started` → `barge_in.armed` — a barge-in capture opens **while the
   assistant speaks** (so Ali can cut in).
2. `tts.completed` (state → idle) — that barge-in capture **tears down**.
3. ~130 ms later → `listening.started` — a **fresh** capture opens for Ali's
   reply.
4. `listening.stopped` on trailing silence.

That teardown-and-reopen at step 2→3 happens **every single turn**, right at the
moment Ali goes to reply. That stop/restart is the seam he feels: "the second I
stop talking you kill the mic and open a new one."

## Two things NOT to do (learned the hard way this session)

- **Do NOT switch input to the built-in MacBook mic** to "simplify." The MacBook
  mic's end-of-speech detection is poor — Vox never detected that Ali finished
  talking and kept listening. Ali switched to **HUAWEI FreeClip 2** earbuds
  (Bluetooth, 16 kHz HFP input) *specifically because* their cleaner mic signal
  makes trailing-silence endpointing actually fire. The earbuds are the fix, not
  the problem. Keep them as the input device; do not degrade endpointing.
- **Do NOT blame Bluetooth profile switching.** It was considered and ruled out
  by Ali — it's not the seam. The seam is the open-per-turn teardown above.

## Design details to resolve while implementing

- **Hotkey specifics:** which combo, and whether it's a **global** macOS hotkey
  (works even when the terminal isn't focused — needed since Ali may be in a
  browser) vs terminal-scoped. Global is almost certainly required.
- **Toggle vs hold:** tap-to-toggle (open until tapped again) is the assumed
  default. Consider whether a hold-to-talk variant is also wanted. Start with
  toggle.
- **Feedback when the gate flips:** Ali needs to know the current gate state —
  a short earcon, a TTS confirmation, and/or a persistent on-screen indicator.
  Silent state changes will confuse.
- **Interaction with barge-in:** the persistent stream should still support
  barge-in (interrupting TTS) — but only when the gate is open. Keep echo
  cancellation / `barge_in.echo_suspected` handling intact so the earbud mic
  doesn't transcribe Vox's own TTS.
- **Existing `voice_session` mute/unmute:** there are already `mute`/`unmute`
  actions — check whether they currently stop capture (wrong for this design) or
  just gate it (right). Reuse/refactor rather than adding a parallel concept.
- **Local-only constraint stays:** Whisper + Kokoro, no cloud speech.

## Third failure mode: mic false-opens and closes on echo/transients

Observed at the end of the session: after a TTS reply, the listen window opened
~139 ms after `tts.completed`, closed after **0.72 s** on `trailing_silence` with
only **0.08 s** of "speech", and produced `stt.non_speech` (no real words). It
then reopened and did the same thing ~1 s later. To the user the mic "kept
opening and closing" and never gave a real window — he was NOT quiet.

**Candidate causes — UNVERIFIED, agent must confirm which is real:**
- **Speech fell in a dead gap.** Two listen windows (a `converse` then a
  follow-up `listen`) opened/closed ~5 s apart; Ali says he talked "in the first
  half and the second half" and it got split. His words may have landed in the
  ~5 s window when the mic was CLOSED between the two captures — so it only
  caught fragments (`stt.non_speech`). This is Ali's own theory and fits the log.
- **Echo tail of Vox's TTS.** Original hypothesis — WEAK: Ali uses closed
  Bluetooth earbuds, so TTS plays into his ears, not acoustically into the mic.
  Acoustic echo is unlikely with earbuds. Do not assume this without proof.
- **Bluetooth transient.** A pop when the FreeClip mic profile activates could
  register as a ~0.1 s onset. Plausible; verify.

Whatever the cause, the observable bug is: a listen turn ended as
`trailing_silence` with near-zero real speech, twice, and Ali got no usable
window. Reproduce and confirm the actual mechanism before fixing.

How the new design fixes it — make sure the implementation does all of these:
- **Key press marks turn start.** With push-to-talk / tap-to-start, echo and
  transients can't open or end a turn on their own — only the user's key can.
  This is the primary fix.
- **Post-TTS guard window.** After `tts.completed`, ignore mic input for a short
  guard (e.g. 250–400 ms) so the TTS echo tail can't register as onset.
- **Require real speech onset, not a transient.** Don't count a sub-~200 ms blip
  as onset; wait for sustained speech before starting the trailing-silence timer.
  Never end a turn as `trailing_silence` when total captured speech is near zero
  — that's a false open, not a finished turn.
- **Keep/strengthen echo cancellation** so the earbud mic doesn't hear Vox
  (`barge_in.echo_suspected` already fires; lean on it).

## Deliverable (Feature 1 — persistent mic + gate)

Implement in `~/vox-mcp`: persistent capture, the gate + default-closed state,
the global keyboard toggle, and state feedback. Verify against the same failure
the log showed — no `listening.started`/teardown churn between turns while the
gate stays open.

---

# Feature 2 — Flow-style system-wide dictation (replace Wispr Flow)

Ali wants to drop Wispr Flow and own the equivalent inside Vox: press a hotkey,
talk, and **polished text drops at the cursor in whatever app is focused**
(browser, Notes, Slack, editor — anywhere, not just Claude Code). Local-only.

## Non-negotiable framing (Ali was emphatic)

- **One-way. No agent, no TTS, nothing talks back.** This is pure dictation:
  mic → text at cursor. There is NO assistant reply, NO conversation, NO Kokoro
  speech. (Contrast Feature 1, which is the two-way agent conversation.)
- **MCP-independent. Runs at the Vox runtime level, not through any MCP host.**
  It must work with NO MCP client connected — standalone, system-wide, in any
  app (Chrome, Notes, anything). It is NOT a Claude Code feature; Ali may happen
  to use it while in Claude Code, but that's incidental and must not be assumed.
- **Hold-to-talk.** Press and HOLD the keybind → mic starts capturing; RELEASE
  the keybind → stop, transcribe, insert the text. The release is the end signal
  (no VAD/silence detection needed — same robustness win as Feature 1's key
  boundary). That's the whole interaction; keep it that simple.

## What Wispr Flow is (researched 2026-07-27 — facts, with sources below)

- **Global hotkey**, default **hold Fn** on Mac. Two modes: push-to-talk (hold)
  and hands-free (double-tap the shortcut to start/stop).
- Inserts **cleaned, formatted** text at the cursor in **any** app.
- **Cloud-only** STT (OpenAI subprocessor) + a **fine-tuned Llama cleanup pass**
  that removes filler words, adds punctuation, formats paragraphs. No offline
  mode; ~700 ms p99, feels 1–2 s due to network round-trip.
- "Voice shortcuts": a spoken trigger inserts a saved text/code snippet.

## Why Vox already has most of it

Vox has the two hard pieces: **local Whisper STT** and (from Feature 1) a
**global hotkey + capture pipeline**. Local also *beats* Wispr on privacy
(nothing leaves the machine) and latency (no cloud round-trip), with no
subscription. The main new capability is inserting text into the focused app.

## The one new piece — text injection at the cursor (macOS)

- **Clipboard + synthesized ⌘V** (save clipboard → set text → paste → restore
  original). Simplest, most reliable; what SuperWhisper and similar local tools
  do. **Recommended default.**
- **Direct keystroke synthesis** (`CGEventCreateKeyboardEvent`) — more native
  but slow for long text and flaky with special characters.
- **Accessibility API** (`AXUIElement` set value / insert) — most robust, most
  complex.
- All of these require macOS **Accessibility permission** for the Vox process.

## Architecture fit

Flow is a **self-contained path in the Vox runtime daemon**, not a route through
the MCP conversation layer:

`hold key → capture → Whisper STT → (optional local cleanup) → inject text at cursor`

- It shares the low-level pieces with Feature 1 — the global hotkey mechanism,
  audio capture, and local Whisper — but it does NOT touch the agent/MCP
  conversation flow. No TTS, no assistant turn. When the key is released, the
  transcript goes straight to the focused app.
- It must run whenever the Vox runtime is up, with **zero MCP clients
  connected**. Do not gate it behind an active voice session/agent.
- Cleanup (filler removal / punctuation / formatting) is an optional pass on the
  STT output: fast rule-based, or a small local LLM for Wispr-quality polish.
  Keep it local per Vox's no-cloud-speech rule. Must add negligible latency.

## VERIFY — proposals, not confirmed against the code

The author of this brief has NOT read the `vox-mcp` source. Before building:
- Confirm how the current capture loop is structured and whether a "sink"
  abstraction fits cleanly (vs. a bigger refactor).
- Confirm whether the Vox process already holds macOS Accessibility permission,
  and what's needed to synthesize ⌘V / keystrokes.
- Decide clipboard-paste vs keystroke vs AX by testing insertion into real apps
  (browsers and Electron apps are the usual trouble spots).
- Decide whether cleanup is rule-based or a local LLM, and whether it's on by
  default (Wispr's polish is a big part of why it "feels" good).

---

# Feature 3 — Read selected text aloud, verbatim (the reverse of Feature 2)

Completes the cycle: F2 is voice→text, F3 is text→voice. Ali frequently has text
on screen he wants read to him. Today he has to find Gemini / some assistant,
paste it, beg it not to rephrase, and trigger its TTS — and they paraphrase
anyway. That's the pain to kill.

## Non-negotiable framing

- **Verbatim. NO LLM in the path. NO paraphrasing, summarizing, or "cleanup."**
  The selected text goes **straight to Kokoro TTS** exactly as written. This is
  the entire point of the feature — do not route it through any model that could
  reword it.
- **MCP-independent, system-wide, runtime-level** — same as Feature 2. Works in
  any app with zero MCP clients connected. Not a Claude Code feature.
- **Keybind-triggered.** Can be the same keybind family as F2 (e.g. a distinct
  combo, or a modifier variant). Ali is open on the exact binding.

## Interaction

1. Ali selects text in any app.
2. He presses the read-aloud keybind.
3. Vox captures the current selection, then speaks it verbatim via Kokoro.
4. Pressing the keybind again (or a stop key) stops playback. Barge-in / stop
   handling already exists in Vox — reuse it.

## The one new piece — capturing the current selection (macOS)

- **Synthesize ⌘C → read clipboard → restore original clipboard.** Simple,
  works everywhere. Clobbers the clipboard momentarily; save/restore it.
- **Accessibility API `AXSelectedText`** on the focused element — cleaner (no
  clipboard clobber) but doesn't work in every app (some don't expose it).
- Practical approach: try AX first, fall back to ⌘C snapshot. Needs the same
  macOS **Accessibility permission** as Feature 2.

## Architecture fit

`keybind → capture selection → Kokoro TTS (verbatim) → playback`. Reuses Vox's
existing Kokoro path and stop/barge-in handling; the only new capability is
reading the OS selection. Like F2, it lives in the runtime daemon, independent
of the MCP/agent layer.

## VERIFY — proposals, not confirmed against the code

Author has not read `vox-mcp`. Before building: confirm the selection-capture
method against real apps (browsers/Electron), confirm Accessibility permission
state, and confirm the Kokoro playback path can be invoked directly (no agent
turn) from a standalone runtime trigger.

## Shared across Features 2 & 3

Both are standalone, MCP-independent, keybind-driven, system-wide, and reuse
Vox's local STT/TTS. Consider one small "Vox hotkey daemon" that owns all global
keybinds (Feature 1 gate toggle, Feature 2 dictation, Feature 3 read-aloud) and
routes to the right handler — rather than three separate hotkey implementations.

## Sources
- https://wisprflow.ai/features
- https://docs.wisprflow.ai/articles/2612050838-supported-unsupported-keyboard-hotkey-shortcuts
- https://tldv.io/blog/wisprflow/
- https://lumevoice.com/blog/superwhisper-vs-wispr-flow-comparison-2026/
- https://openwhispr.com/blog/local-vs-cloud-transcription
