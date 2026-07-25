# PLAN — verify what is built, then finish proactive input

Read `STATUS.md` first for what the runtime does today. This file is the
next-step plan and stands alone.

Two of the three items below are **verification, not construction**. Everything
in steps 1 and 2 is already written, committed, and passing tests — it has
simply never been used by a human. In the session that built it, three bugs
(an echo gate that did nothing, a noise floor that cut sentences off mid-word,
a scope check that answered code questions) were all already marked done, and
all three were found by *talking to the thing*. Do not trust green.

Ground rules for this repo: commit straight to `main`, never branch. Deploy with
`zsh scripts/install_macos_app.sh` (Swift rebuild + app install + runtime
restart); a Python-only change needs just
`launchctl kickstart -k gui/$(id -u)/com.vox.runtime`. Settings go in
`~/.vox/settings.json` via `vox set NAME=value` — **not** `launchctl setenv`,
which never reaches the runtime. Tests: `.venv/bin/python -m pytest tests/`
(276 passing at handoff).

---

## 1. Verify barge-in on headphones

**Why it is blocked today:** measured on this MacBook, Kokoro through the
built-in speakers returns into the mic at −22 dBFS p90 while Ali's own voice
peaks at −29.8. He is ~24 dB *quieter than his own echo*, so no threshold
separates them. Barge-in therefore reads the default output device and refuses
to arm unless it is recognisably headphones — `VoxEngine.barge_in_availability()`
in `src/voxmcp/engine.py`.

With AirPods or wired headphones set as system output:

```bash
vox set VOX_BARGE_IN_ENABLED=1          # writes settings.json, restarts runtime
curl -s http://127.0.0.1:8766/health | python3 -m json.tool | grep -i barge
```

`diagnostics(section="privacy").barge_in.available` must be `true` with the
headphone device named. If it says `shared_output`, macOS has not actually
switched output — fix that first.

Then by voice, in order:

1. **No false interrupts.** Have the agent speak three long replies while you
   stay silent. Barge-in must not fire once. If it does, run
   `vox barge-in calibrate` (needs a real terminal — `click.pause` will not work
   when driven by an agent) and apply the printed
   `VOX_BARGE_IN_MAX_VAD_MARGIN_DB` with `vox set`.
2. **Interrupt mid-sentence.** Talk over a long reply. Acceptance: playback dies
   within ~0.2 s, `spoken.status == "barge_in"`, and **the transcript contains
   the first word you spoke** — that proves the pre-roll splice and is the whole
   point of the design.
3. **The panel is honest.** While the agent speaks, the menu-bar glyph should be
   red on the `waveform.badge.mic` symbol and the panel should read
   **"Speaking · cut in"**. If not, the Swift half was not rebuilt — run
   `zsh scripts/install_macos_app.sh`.
4. **Cancel while armed.** `vox control cancel` mid-speech, then confirm
   `/health` shows `microphone_open: false` and no stuck mic.

**If step 1 fails even after calibration**, the honest outcome is that barge-in
stays off and the Reply button / ⌃⌥⌘R remains the interruption path. Record that
in `STATUS.md` rather than leaving it looking shippable.

## 2. Verify the companion past two turns

Live-proven so far: one small-talk answer (3.1 s) and one correct escalation.
Untested: the loop, the spoken controls inside it, and the interview path.

```bash
vox set VOX_COMPANION_ENABLED=1
```

Call the `companion` MCP tool with `budget_turns: 6` and:

- Hold **four or five** small-talk turns. Acceptance: each answers in ~2–4 s in
  a voice different from the agent's, and `turns[].said` is populated.
- Say **"stop"** mid-loop. Acceptance: `reason: "user_stopped"`, stops
  listening.
- Ask something about the code. Acceptance: `status: "escalated"`,
  `reason: "out_of_scope"`, `turns[].said` is `null` (the backend must not be
  called at all), and `transcript` carries everything heard.
- Kill the backend (`mv ~/grokctl ~/grokctl.off`) and hand over again.
  Acceptance: escalates with the grokctl failure reason and does **not** hang.
  Restore afterwards.

Scope lives in `companion_may_answer` (`src/voxmcp/intents.py`). It requires a
positive small-talk *signal*. If it wrongly refuses something harmless, add the
phrase to `_SMALL_TALK_SIGNAL` **and** to `ANSWERABLE` in
`tests/test_companion.py`. If it wrongly *answers* something about the work,
that is the serious direction — add to `_WORK_TOPIC` and to `MUST_ESCALATE`.

Then run the interview path once:
`voice_survey(agent="companion", turns=[...])` with three scripted questions. It
should read them in the companion voice and return a transcript. This is the
structured-elicitation use case and it has never been run.

## 3. Type-while-listening fusion (construction)

The remaining unbuilt feature, in Ali's words:

> "sometimes I wish you are able to read my text at the same time I send it when
> you are expecting me to speak. I need to wait for you to listen to everything
> I said, then you will get the message I sent — which just wastes a turn."

A running MCP tool call blocks the host turn, so typed text sits queued until
`listen` returns. The 75 s cap shortens the dead wait; it does not fix it.

**Route A — the Vox-side primitive (build this).** A `deliver_text` control
action that ends an in-flight listen early and returns the typed string:

- `src/voxmcp/mcp_server.py`: add `deliver_text` to the `/control` allowed-action
  list and to `dispatch_control`, carrying a `text` argument.
- `src/voxmcp/audio.py`: `CaptureControl` gains `deliver_text(value)` setting a
  `threading.Event` plus the string, alongside the existing `cancel` /
  `end_utterance` / `interrupt` events. `capture()`'s loop already polls those
  each iteration — add the same check and stop with a new
  `CaptureStopReason.DELIVERED_TEXT`.
- `src/voxmcp/engine.py`: `_capture_once` skips Whisper for that reason and
  returns the supplied text with `status: "delivered_text"`. If no listen is
  active it is a no-op.
- Tests: extend the fake-recorder pattern in `tests/test_engine.py` — a listen
  that receives `deliver_text` returns the typed string, runs no STT, and leaves
  the session idle.

**Route B — host glue (probably not buildable).** Something must POST
`deliver_text` when Ali submits a prompt while a listen is in flight. That needs
a Claude Code pre-submit hook that can fire during an active tool call. If none
exists, ship the primitive and stop — a manual hotkey can still POST it.
**Do not ship a polling hack** that guesses when Ali is typing.

---

## Known rough edges (fix if you are already in there)

- `vox barge-in calibrate` uses `click.pause()`, so only a human in a terminal
  can drive it.
- `installer.py` does not create `~/.vox/settings.json`. Missing means defaults,
  which is correct, but a fresh install has no example to copy.
- `initial_noise_floor_dbfs` only matters for the first ~0.25 s before the
  rolling window fills. Harmless, slightly vestigial.
- The voice turn contract is a prompt, not a mechanism. It cut the mean agent
  turn from 30.2 s to 18.1 s but one exchange still ran 26 s. Nothing enforces
  it.
