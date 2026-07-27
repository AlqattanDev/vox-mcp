# PLAN — verify the voice suite aloud, then finish proactive input

Read `STATUS.md` first for what the runtime does today. This file is the
next-step plan and stands alone.

**Everything below is verification, not construction.** The voice suite (gate,
turn key, dictation, read-aloud) is written, committed, pushed, and green — and
most of it has never been used by a human. This repo has shipped three
green-test bugs that only speaking aloud found, and this suite has already
added two more that no test caught: a 0.5 s open guard that was measured to be
too short, and a second key tap that got swallowed during warm-up. Do not trust
green.

Ground rules for this repo: commit straight to `main`, never branch. Deploy with
`zsh scripts/install_macos_app.sh` (Swift rebuild + app install + runtime
restart); a Python-only change needs just
`launchctl kickstart -k gui/$(id -u)/com.vox.runtime`. Settings go in
`~/.vox/settings.json` via `vox set NAME=value` — **not** `launchctl setenv`,
which never reaches the runtime. Tests: `.venv/bin/python -m pytest tests/`
(387 passing at handoff).

**The build now fails without a persistent codesigning identity.** That is
deliberate — see `STATUS.md` → Deploy. If it stops you, fix the identity rather
than reaching for `VOX_ALLOW_ADHOC_SIGN=1`, or Accessibility dies every install.

---

## 0. Grant Accessibility (one click, blocks §3 and §4)

Press **⌃⌥⌘D** once. macOS opens System Settings › Privacy & Security ›
Accessibility with Vox listed. Enable it. Until then dictation and read-aloud
report themselves unavailable in the panel — they do not fail silently.

Confirm it stuck across a deploy:

```bash
zsh scripts/install_macos_app.sh
codesign -dv --verbose=2 ~/Applications/Vox.app 2>&1 | grep TeamIdentifier
```

`TeamIdentifier` must stay `YN9839UZF5`. If it ever reads `adhoc`, the grant is
gone and the build guard failed to stop it.

## 1. The turn key on real hardware

Already verified without a voice: one `capture.stream_opened` across three
turns, zero `listening.started` through 30 s of speech with the gate shut, zero
phantom windows on a cold open, and pause/stop/`deliver_text` unchanged.

What needs your voice, with the FreeClip connected:

1. **A long turn with pauses.** ⌃⌥⌘L, talk for ~60 s with several multi-second
   thinking pauses, ⌃⌥⌘L. Acceptance: **exactly one** turn, the transcript is
   complete, and nothing endpointed mid-thought. This is the whole reason the
   gate-open turn runs with `onset_timeout_s=None`.
2. **The cue is honest.** The rising blip must land *before* you start talking
   and after the mic is genuinely live. If the first word of a session is ever
   clipped, the 1.0 s guard is being waited out in the wrong place.
3. **Barge-in still works.** With `VOX_BARGE_IN_ENABLED=1` and headphones, talk
   over a long reply. Acceptance: playback dies within ~0.2 s,
   `spoken.status == "barge_in"`, and the transcript contains **the first word
   you spoke** — that proves the pre-roll survived the move to the shared
   stream, and it now attaches to the live stream instead of opening a second
   one. Confirm no `capture.stream_opened` appears when arming.
4. **Read the log, not the vibe.**

```bash
python3 - <<'EOF'
import json
rows = [json.loads(l) for l in open('/Users/ali/.vox/state/events.jsonl')][-200:]
opens = [r for r in rows if r['event'] == 'capture.stream_opened']
phantom = [r for r in rows if r['event'] == 'listening.stopped'
           and (r.get('data') or {}).get('speech')
           and ((r.get('data') or {}).get('duration_s') or 0) < 1.5]
print('stream opens:', len(opens), '| phantom windows:', len(phantom))
EOF
```

One open per session, zero phantom windows.

## 2. Dictation, in the apps you actually use

Hold **⌃⌥⌘D**, speak, release. The text should land at the cursor.

- Chrome address bar, a Google Doc, Notes, Slack, and a terminal.
- **Put an image on the clipboard first**, dictate, then paste. The image must
  still be there — text-only restore would look fine and be wrong.
- **Arabic.** This is why injection is clipboard+⌘V rather than synthesized
  keystrokes; if Arabic is mangled the decision was wrong, not the tuning.
- Release-to-visible under ~1.5 s for a 10 s utterance.
- With Claude Code **quit**, it must still work. That is the point of dictation
  living at the runtime level.

If the text lands but the filler-stripping is wrong for how you talk, tune the
`_FILLERS` list in `src/voxmcp/dictation.py` (and its test), or set
`vox set VOX_DICTATION_CLEANUP=off` for raw Whisper output.

## 3. Read-aloud, verbatim

Select text, press **⌃⌥⌘S**. Press again to stop.

- Chrome, Notes, a PDF in Preview, and a terminal. AX read is tried first and
  returns nothing in some Chromium/Electron surfaces; the ⌘C fallback covers
  those, and your clipboard must be intact afterwards.
- **Spot-check verbatim on paraphrase-prone material**: numbers, version
  strings, names, a line of code. Nothing on this path can rewrite them, so any
  drift is Kokoro's pronunciation, not a model — different bug, different fix.
- With an agent already speaking, ⌃⌥⌘S must **queue**, not cut in.
- Nothing selected → the error earcon, not silence.

## 4. Type-while-listening: confirm the hook fires

The last unfinished item from the previous plan. `deliver_text` is built and
verified end to end at the runtime level, and it works through the gate (there
is a test for it — the frame source checks `text_delivered`, which it did not
originally, and typed turns would have been lost).

What is unproven is the **host** half: `.claude/settings.json` →
`scripts/claude_code_deliver_text.sh` is a `UserPromptSubmit` hook, and hooks
load at session start. So: start a **fresh** Claude Code session, get a listen
in flight, and type instead of speaking. Acceptance: the listen returns
immediately with `backend: delivered_text` and no Whisper call.

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
- A session-lived stream keeps the headset in the 16 kHz HFP call profile for
  as long as the session is up, so Kokoro sounds like a phone call until you
  pause or stop. This was a deliberate trade for killing the per-turn transient;
  if it grates, the alternative is closing the stream during playback, which
  reintroduces one stream-open per turn and is incompatible with barge-in.
- The voice turn contract is a prompt, not a mechanism. It cut the mean agent
  turn from 30.2 s to 18.1 s but one exchange still ran 26 s. Nothing enforces
  it.
