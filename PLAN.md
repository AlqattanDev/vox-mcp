# PLAN — type-while-listening fusion

## The problem (Ali's words)

> "sometimes I wish you are able to read my text at the same time I send it when
> you are expecting me to speak. In that scenario, I need to wait for you to
> listen to everything I said, then you will get the message I sent (a lot of
> times you will say you didn't get it, then automatically it will be sent to
> you, which just wastes a turn on nothing)."

When Vox is listening (mic open, blocking on `listen`/`converse`) and Ali
decides to **type** instead of speak, the typed message sits queued in Claude
Code until the `listen` tool call returns. The agent then reports "didn't get
it," the queued text auto-sends, and a whole turn is wasted.

## Why it's not a one-file fix

A running MCP tool call blocks the host turn. Claude Code does **not** surface
"the user typed while a tool call is in flight" to any hook in real time, and it
cannot deliver a new user message until the current tool returns. So the typed
text cannot, today, interrupt the `listen` from the host side.

The endpointing cap (now 75s, was 300s) already shortens the dead wait, but the
real fix needs a signal path from "Ali typed" → "end the open listen now."

## Design (two viable routes)

### Route A — external deliver signal the listen watches (self-contained)
1. **MCP primitive:** add control action `deliver_text` to the `/control` route
   and `dispatch_control` (`src/voxmcp/mcp_server.py`) → engine
   `voice_control`. It sets an event on the active `CaptureControl` (like
   `manual_end`) so the in-flight `listen` returns **early** with
   `status: "delivered_text"` and echoes the supplied text as the turn input
   (no STT run). If no listen is active, it's a no-op.
2. **Engine:** `_capture_once` already races `CaptureControl`; add a
   `deliver_text` path alongside `interrupt`/`manual_end` that carries the text
   back out through the operation result.
3. **Host glue (Claude Code):** a small hook or wrapper that, when Ali submits a
   prompt, POSTs `{"action":"deliver_text","text":"..."}` to
   `http://127.0.0.1:8766/control` (with the control token) **before** the
   prompt is processed. This is the hard part — it depends on Claude Code
   exposing a pre-submit hook that can fire while a tool call is active. If it
   can't, Route A can't be triggered automatically (a manual hotkey could still
   POST it).

### Route B — host feature (cleanest, not ours to build)
Claude Code gains the ability to cancel/interrupt a running MCP tool call when
the user submits queued input, returning that input as the result. This is a
host feature request; Vox would just need `listen` to accept an injected result.

## Recommendation
Build the **MCP primitive from Route A** (small, testable, useful on its own —
the menu bar's "I'm done talking" already proves the early-return path works),
then wire the host glue only if/when Claude Code exposes a usable pre-submit
hook. Do **not** ship a fragile polling hack that guesses when Ali is typing.

## Already shipped (context)
Endpointing 1.6s trailing / 75s cap, listen earcons, adaptive glyph menu bar
with one-click stop + direct modes, crash-proof `INTERRUPTED` recovery, streamed
TTS, `/speak` + `/listen` commands. See `git log` and `STATUS.md`.
