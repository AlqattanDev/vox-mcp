# PLAN — verify the voice suite aloud, then finish proactive input

Read `STATUS.md` first for what the runtime does today. This file is the
next-step plan and stands alone.

**Everything below is verification, not construction.** The voice suite (device
lifetime, the ⌘§ key, dictation, read-aloud, the pill) is written, committed,
pushed, and green — and most of it has never been used by a human. This repo has
shipped several green-test bugs that only speaking aloud found, and the suite has
already added more that no test caught: a 0.5 s open guard measured to be too
short, a second key tap swallowed during warm-up, and a dictation that handed a
shared capture stream back still open so the macOS microphone indicator never
went out. Do not trust green.

Ground rules for this repo: commit straight to `main`, never branch. Deploy with
`zsh scripts/install_macos_app.sh` (Swift rebuild + app install + runtime
restart); a Python-only change needs just
`launchctl kickstart -k gui/$(id -u)/com.vox.runtime`. Settings go in
`~/.vox/settings.json` via `vox set NAME=value` — **not** `launchctl setenv`,
which never reaches the runtime. Tests: `.venv/bin/python -m pytest tests/`
(400 passing at handoff).

**The build fails without a persistent codesigning identity.** That is
deliberate — see `STATUS.md` → Deploy. If it stops you, fix the identity rather
than reaching for `VOX_ALLOW_ADHOC_SIGN=1`, or Accessibility dies every install.

---

## 0. Grant Accessibility (one hold, blocks §3 and §4)

Hold **⌘§** for a second. macOS opens System Settings › Privacy & Security ›
Accessibility with Vox listed. Enable it. Until then dictation and read-aloud
report themselves unavailable in the panel — they do not fail silently.

Confirm it stuck across a deploy:

```bash
zsh scripts/install_macos_app.sh
codesign -dv --verbose=2 ~/Applications/Vox.app 2>&1 | grep TeamIdentifier
```

`TeamIdentifier` must stay `YN9839UZF5`. If it ever reads `adhoc`, the grant is
gone and the build guard failed to stop it.

## 1. The microphone indicator, which is the point

Already verified without a voice, against the deployed app on the real FreeClip:
device closed at rest → tap → `device=True gate=False` (warm-up) → `gate=True
mic=True` → close → released, with exactly **one** `capture.stream_opened` and
**one** `capture.stream_closed`. Dictation releases the device on both the
key-released and max-duration paths. Read-aloud opens no input device at all
across a whole spoken reply.

What needs your eyes:

1. **The orange dot goes out.** After a turn ends it should disappear within
   ~2 s, and stay gone the entire time an agent is working. If it lingers, the
   linger is the knob: `vox set VOX_STREAM_IDLE_RELEASE_SECONDS=0.5`.
2. **It comes back only when Vox can hear you.** During a turn, during a
   dictation hold, and during an agent reply with barge-in armed. Never
   otherwise.
3. **Read the log, not the vibe.**

```bash
python3 - <<'EOF'
import json
rows = [json.loads(l) for l in open('/Users/ali/.vox/state/events.jsonl')][-200:]
for k in ('capture.stream_opened', 'capture.stream_closed'):
    print(k, sum(r['event'] == k for r in rows))
EOF
```

Opens and closes must be equal, and both should roughly equal the number of
turns you took.

## 2. The ⌘§ key on real hardware

The key's contract: **a tap never opens the microphone** — it reads the
selection aloud (or stops speech, or ends an agent's already-open turn).
Listening happens only while the key is held.

1. **Tap and hold do not get confused.** A quick tap must never start a
   dictation, and a deliberate hold must never read the selection. The threshold
   is 350 ms (`HotKeyBinding.holdThreshold`). If a tap you meant as a tap starts
   dictating, raise it; if a hold you meant as a hold reads aloud, lower it.
2. **A tap with the room silent and nothing selected must not listen.** The
   acceptance is the absence: no earcon, no red pill, no orange dot — just the
   "Nothing is selected to read" notice.
3. **The reply window is 5 s.** After an agent finishes speaking (via
   `converse`) the mic opens for about five seconds and then gives up. Long
   enough to draw breath and answer; if it is not, `onset_timeout` is per-call
   and the skill can pass more. A ⌘§ tap during that window ends the turn.
4. **Barge-in still works.** With `VOX_BARGE_IN_ENABLED=1` and the earbuds, talk
   over a long reply. Acceptance: playback dies within ~0.2 s,
   `spoken.status == "barge_in"`, and the transcript contains **the first word
   you spoke** — that proves the pre-roll survived. Confirm the armed capture and
   the listen that follows it share **one** stream open, not two.

## 3. Dictation, in the apps you actually use

Hold **⌘§**, speak, release. The text should land at the cursor.

- Chrome address bar, a Google Doc, Notes, Slack, and a terminal.
- **The transcript must still be on the clipboard afterwards.** Dictate, then
  ⌘V somewhere else — the same text should paste. This is the recovery path when
  the injection had nowhere to land, and it is the reason the previous pasteboard
  is no longer restored.
- **No dead first second.** The gate opens immediately on a hold, so speaking the
  instant you press should lose nothing.
- **Arabic.** This is why injection is clipboard+⌘V rather than synthesized
  keystrokes; if Arabic is mangled the decision was wrong, not the tuning.
- Release-to-visible under ~1.5 s for a 10 s utterance.
- With Claude Code **quit**, it must still work. That is the point of dictation
  living at the runtime level.

If the text lands but the filler-stripping is wrong for how you talk, tune the
`_FILLERS` list in `src/voxmcp/dictation.py` (and its test), or set
`vox set VOX_DICTATION_CLEANUP=off` for raw Whisper output.

## 4. Read-aloud, verbatim and deaf

Select text, tap **⌘§**. Tap again to stop.

- Chrome, Notes, a PDF in Preview, and a terminal. AX read is tried first and
  returns nothing in some Chromium/Electron surfaces; the ⌘C fallback covers
  those, and your clipboard must be intact afterwards.
- **The orange dot must never appear.** Verified in tests and live, but this is
  the privacy claim most worth trusting your own eyes on.
- **Spot-check verbatim on paraphrase-prone material**: numbers, version
  strings, names, a line of code. Nothing on this path can rewrite them, so any
  drift is Kokoro's pronunciation, not a model — different bug, different fix.
- With an agent already speaking, a ⌘§ tap **stops the speech** — reading a new
  selection needs a second tap once the room is quiet.
- Nothing selected → the error earcon, not silence.

## 5. The pill

- **Warming** (dim, flat bars) for the ~1 s between the tap and the blip, then
  **listening** (red, bars moving with your actual voice), then gone.
- **The waveform must track your voice, not tick like a clock.** It receives
  ~50 Hz of measured level in bursts of four or five per poll, so loud syllables
  and pauses should be visibly distinct. If it looks like a slow staircase, the
  burst is not arriving — check `curl -s '127.0.0.1:8766/health?levels_since=0'`
  during a hold and confirm `mic_levels` has several entries and a rising
  `mic_levels_seq`.
- **Run `vox doctor` while dictating.** The waveform must not stutter or freeze:
  reading levels is non-destructive precisely so a second `/health` caller cannot
  steal them.
- **The first word of a dictation must survive.** The raw path opts out of the
  open guard; if the opening syllable is ever missing, the callback is dropping
  frames again.
- **Dictating** is teal, not red. Glance at it mid-hold and confirm you can tell
  at a glance that the words are going to the cursor rather than to an agent.
- **Speaking** is blue and pulses. It is deliberately not a level meter.
- **It must never steal focus.** Dictate into a text field while the pill is up;
  if the insertion point moves or the frontmost app deactivates, the panel's
  `.nonactivatingPanel` contract is broken and dictation will paste into the
  wrong place.
- It should follow you across spaces and appear over full-screen apps.
- `vox set VOX_HUD=0` turns it off.

## 6. Type-while-listening: confirm the hook fires

The last unfinished item from the previous plan. `deliver_text` is built and
verified end to end at the runtime level, and it works through the gate.

What is unproven is the **host** half: `.claude/settings.json` →
`scripts/claude_code_deliver_text.sh` is a `UserPromptSubmit` hook, and hooks
load at session start. So: start a **fresh** Claude Code session, get a listen
in flight, and type instead of speaking. Acceptance: the listen returns
immediately with `backend: delivered_text` and no Whisper call — and your
clipboard is **untouched**, because text you typed is already yours.

If the hook does not fire during a running tool call, `vox control deliver-text`
stays the manual path. **Do not ship a poller** that guesses when you are typing.

---

## Known rough edges (fix if you are already in there)

- `vox barge-in calibrate` uses `click.pause()`, so only a human in a terminal
  can drive it.
- `installer.py` does not create `~/.vox/settings.json`. Missing means defaults,
  which is correct, but a fresh install has no example to copy.
- The FreeClip cancels its own speaker out of the mic feed (−61 dBFS measured),
  so playing audio through it can never test capture. Speak, or use a separate
  output device.
- Releasing the device per turn means the earbuds renegotiate A2DP→HFP on each
  one, which is a faint click at the start of a turn. The trade is deliberate:
  Kokoro is full-quality between turns instead of sounding like a phone call all
  session, and the indicator is honest. `VOX_STREAM_IDLE_RELEASE_SECONDS` is the
  dial if the click matters more than the dot.
- ⌘§ is registered globally, so no other app can use it. Nothing on this machine
  did.
- The voice turn contract is a prompt, not a mechanism. It cut the mean agent
  turn from 30.2 s to 18.1 s but one exchange still ran 26 s. Nothing enforces
  it.
