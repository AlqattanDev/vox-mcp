"""The page the phone opens to become Vox's microphone and speaker.

It is one file with no build step, no dependency and no external fetch, because
the thing it exists to rescue is a laptop nobody can look at: whatever this page
needs has to already be inside the runtime that is still running.

The browser does three jobs and no more — capture at 16 kHz, ship int16 PCM,
play the wavs it is sent — so the endpointing, the STT and the TTS all stay on
the Mac where they already work.
"""

from __future__ import annotations


PHONE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Vox</title>
<style>
  :root {
    --bg: #f6f6f7; --fg: #16181d; --muted: #6b7280;
    --card: #ffffff; --line: #e3e4e8; --accent: #2f6df6; --live: #14915b; --warn: #b4451f;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0e0f12; --fg: #ecedf1; --muted: #9aa0ac;
      --card: #16181d; --line: #24272e; --accent: #6f9bff; --live: #3ecf8e; --warn: #ff8a5c;
    }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; min-height: 100dvh; background: var(--bg); color: var(--fg);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: max(24px, env(safe-area-inset-top)) 20px max(24px, env(safe-area-inset-bottom));
    gap: 20px;
  }
  .card {
    width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--line);
    border-radius: 16px; padding: 22px; display: flex; flex-direction: column; gap: 16px;
  }
  h1 { margin: 0; font-size: 19px; font-weight: 620; letter-spacing: -0.01em; }
  .row { display: flex; align-items: center; gap: 10px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); flex: none; }
  .dot.live { background: var(--live); box-shadow: 0 0 0 4px color-mix(in srgb, var(--live) 22%, transparent); }
  .dot.hot { background: var(--warn); box-shadow: 0 0 0 4px color-mix(in srgb, var(--warn) 22%, transparent); }
  #state { font-size: 15px; }
  #detail { color: var(--muted); font-size: 13px; margin: 0; min-height: 1.4em; }
  button {
    font: inherit; font-weight: 600; border: 0; border-radius: 12px; padding: 15px 18px;
    background: var(--accent); color: #fff; width: 100%; cursor: pointer;
  }
  button.secondary { background: transparent; color: var(--muted); border: 1px solid var(--line); }
  button:disabled { opacity: 0.45; }
  .meter { height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; }
  .meter > i { display: block; height: 100%; width: 0%; background: var(--live); transition: width 90ms linear; }
  input {
    font: inherit; width: 100%; padding: 12px 14px; border-radius: 12px;
    border: 1px solid var(--line); background: var(--bg); color: var(--fg);
  }
  .hint { color: var(--muted); font-size: 12px; text-align: center; max-width: 420px; margin: 0; }
</style>
</head>
<body>
<div class="card">
  <h1>Vox &mdash; phone audio</h1>
  <div class="row"><span class="dot" id="dot"></span><span id="state">Not connected</span></div>
  <div class="meter"><i id="level"></i></div>
  <p id="detail"></p>
  <div id="tokenbox" hidden><input id="token" type="password" placeholder="Vox control token" autocomplete="off"></div>
  <button id="go">Connect</button>
  <button id="stop" class="secondary" hidden>Disconnect</button>
</div>
<p class="hint">The mic only records while Vox is listening. Whisper and Kokoro stay on the Mac.</p>
<script>
(() => {
  "use strict";
  const RATE = 16000;
  const el = (id) => document.getElementById(id);
  const dot = el("dot"), state = el("state"), detail = el("detail"), meter = el("level");
  const go = el("go"), stopBtn = el("stop"), tokenBox = el("tokenbox"), tokenInput = el("token");

  const params = new URLSearchParams(location.search);
  // A token copied out of a wrapped terminal line arrives with spaces in it.
  let token = (params.get("t") || localStorage.getItem("vox.token") || "").replace(/\\s+/g, "");
  if (!token) tokenBox.hidden = false;

  let ws = null, ctx = null, stream = null, node = null, src = null;
  let micWanted = false, acquiring = false, running = false;
  const playing = new Map();
  let wakeLock = null;

  function say(text, mode) {
    state.textContent = text;
    dot.className = "dot" + (mode ? " " + mode : "");
  }
  function note(text) { detail.textContent = text || ""; }

  async function keepAwake() {
    try { wakeLock = await navigator.wakeLock.request("screen"); } catch (e) { /* optional */ }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && running && !wakeLock) keepAwake();
  });

  function connect() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(scheme + "://" + location.host + "/phone/ws?t=" + encodeURIComponent(token));
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      running = true;
      localStorage.setItem("vox.token", token);
      tokenBox.hidden = true;
      go.hidden = true; stopBtn.hidden = false;
      say("Connected \\u2014 idle", "live");
      note("Waiting for Vox. Leave this page open.");
      keepAwake();
    };
    ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.type === "mic") { micWanted = !!msg.open; micWanted ? openMic() : closeMic(); }
      else if (msg.type === "play") play(msg);
      else if (msg.type === "cancel") cancel(msg.id);
    };
    ws.onclose = () => { teardown("Disconnected"); };
    ws.onerror = () => { note("Connection failed. Check the token and that Vox is running."); };
  }

  function teardown(reason) {
    running = false;
    closeMic();
    for (const id of Array.from(playing.keys())) cancel(id);
    if (wakeLock) { try { wakeLock.release(); } catch (e) {} wakeLock = null; }
    ws = null;
    go.hidden = false; stopBtn.hidden = true;
    say(reason, "");
    meter.style.width = "0%";
  }

  async function ensureContext() {
    if (!ctx) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      try { ctx = new Ctor({ sampleRate: RATE }); } catch (e) { ctx = new Ctor(); }
    }
    if (ctx.state === "suspended") await ctx.resume();
    return ctx;
  }

  // Downsample by simple decimation with an averaging window. The phone's own
  // rate is usually already 16k or 48k; averaging keeps 48k from aliasing the
  // room into the band Whisper listens in.
  function toPcm16(input, inRate) {
    const ratio = inRate / RATE;
    const out = new Int16Array(Math.floor(input.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const start = Math.floor(i * ratio), end = Math.min(input.length, Math.floor((i + 1) * ratio));
      let sum = 0, n = 0;
      for (let j = start; j < end; j++) { sum += input[j]; n++; }
      const value = n ? sum / n : 0;
      out[i] = Math.max(-1, Math.min(1, value)) * 32767;
    }
    return out;
  }

  async function openMic() {
    if (stream || acquiring || !running) return;
    acquiring = true;
    try {
      await ensureContext();
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1, echoCancellation: true,
          noiseSuppression: true, autoGainControl: true
        }
      });
      src = ctx.createMediaStreamSource(stream);
      node = ctx.createScriptProcessor(2048, 1, 1);
      node.onaudioprocess = (event) => {
        if (!ws || ws.readyState !== 1 || !micWanted) return;
        const input = event.inputBuffer.getChannelData(0);
        let peak = 0;
        for (let i = 0; i < input.length; i++) { const v = Math.abs(input[i]); if (v > peak) peak = v; }
        meter.style.width = Math.min(100, Math.round(peak * 180)) + "%";
        ws.send(toPcm16(input, ctx.sampleRate).buffer);
      };
      src.connect(node);
      // ScriptProcessor only runs while connected to a destination; a zeroed
      // gain keeps it ticking without feeding the mic back into the speaker.
      const sink = ctx.createGain();
      sink.gain.value = 0;
      node.connect(sink);
      sink.connect(ctx.destination);
      say("Listening", "hot");
      note("Vox has the mic open.");
    } catch (err) {
      note("Microphone blocked: " + err.message);
    } finally {
      acquiring = false;
      if (!micWanted) closeMic();
    }
  }

  function closeMic() {
    if (node) { try { node.disconnect(); } catch (e) {} node.onaudioprocess = null; node = null; }
    if (src) { try { src.disconnect(); } catch (e) {} src = null; }
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    meter.style.width = "0%";
    if (running) { say("Connected \\u2014 idle", "live"); note("Mic off."); }
  }

  function bytesFromBase64(value) {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
  }

  async function play(msg) {
    try {
      await ensureContext();
      const buffer = await ctx.decodeAudioData(bytesFromBase64(msg.wav));
      const source = ctx.createBufferSource();
      const gain = ctx.createGain();
      gain.gain.value = typeof msg.volume === "number" ? msg.volume : 1;
      source.buffer = buffer;
      source.connect(gain);
      gain.connect(ctx.destination);
      source.onended = () => { playing.delete(msg.id); done(msg.id); };
      playing.set(msg.id, source);
      source.start();
    } catch (err) {
      done(msg.id);
    }
  }

  function cancel(id) {
    const source = playing.get(id);
    if (!source) return;
    playing.delete(id);
    try { source.onended = null; source.stop(); } catch (e) {}
  }

  function done(id) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ended", id }));
  }

  go.addEventListener("click", async () => {
    if (!token) {
      token = tokenInput.value.replace(/\\s+/g, "");
      if (!token) { note("Paste the token from: cat ~/.vox/control.token"); return; }
    }
    say("Connecting\\u2026", "");
    // The tap is the gesture that lets audio start at all; spend it here.
    await ensureContext();
    connect();
  });
  stopBtn.addEventListener("click", () => { if (ws) ws.close(); else teardown("Not connected"); });
})();
</script>
</body>
</html>
"""


__all__ = ["PHONE_HTML"]
