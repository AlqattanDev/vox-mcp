"""The phone as Vox's microphone and speaker, over one WebSocket.

Vox reads the Mac's own PortAudio device.  That is the right default and the
wrong one for a laptop with a dead screen sitting at home: the agent, the shell
and the work are all reachable from a phone over Tailscale, and only the voice
is stuck in the room.

Nothing about the pipeline moves.  Whisper and Kokoro still run on this machine
and the audio still never leaves it except to the phone the user is holding, so
``local_only`` stays a literal statement.  What moves is the *device*: a
connected phone becomes the input stream and the player, and every existing
tool — ``converse``, ``listen``, ``speak``, the earcons, barge-in — keeps
working unchanged because none of them ever talked to hardware directly.

The seam is deliberately narrow.  Capture reuses ``PersistentCaptureSource``
whole, gate, open-guard, stall detection and all, by handing it a stream
factory whose "device" is the WebSocket; the endpointer cannot tell the
difference and so needs no second implementation to keep in sync.  Playback
reuses ``PlaybackHandle`` by satisfying the same small process protocol
``afplay`` does — ``poll``/``wait``/``terminate``/``kill`` — so cancellation,
the registry and temp-file cleanup are the code that already works.

One phone at a time.  A second connection replaces the first rather than
mixing two rooms into one microphone.
"""

from __future__ import annotations

import asyncio
import base64
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np


# The phone captures and resamples to this before sending. Whisper wants 16 kHz
# mono anyway, so anything higher would be bandwidth spent on samples the STT
# backend immediately throws away — and this link is a phone on cellular data,
# not a USB cable.
REMOTE_SAMPLE_RATE = 16_000

# How long a playback waits for the phone to say it finished before giving up
# and letting the turn continue. A dropped connection mid-sentence must not
# wedge the engine on a wav that will never end.
PLAYBACK_ACK_GRACE_S = 5.0

# Frames older than this are the past: if a subscriber cannot keep up, drop the
# stale audio rather than let the endpointer run further and further behind the
# person talking.
SUBSCRIBER_QUEUE_FRAMES = 256


class RemoteAudioUnavailable(RuntimeError):
    """Raised when a remote audio path is asked for with no phone attached."""


@dataclass(frozen=True)
class PhoneInfo:
    """What is known about the attached phone, for status and diagnostics."""

    connection_id: str
    user_agent: str
    connected_at: float
    sample_rate: int = REMOTE_SAMPLE_RATE


class _Playback:
    """One wav in flight to the phone."""

    def __init__(self, playback_id: str, duration_s: float) -> None:
        self.id = playback_id
        self.duration_s = duration_s
        self.finished = threading.Event()
        self.cancelled = False


class PhoneLink:
    """The one attached phone, or nothing.

    Every method here is called from two worlds: the daemon's asyncio loop (the
    WebSocket handler) and Vox's worker threads (capture pumps, playback waits).
    The lock covers the state; anything that has to reach the socket is handed
    to the loop with ``call_soon_threadsafe`` rather than awaited from a thread.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._info: PhoneInfo | None = None
        self._send: Callable[[dict[str, Any]], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[queue.Queue[np.ndarray]] = set()
        self._playbacks: dict[str, _Playback] = {}
        self._mic_open = False
        self._chosen = False
        self._on_event: Callable[..., Any] | None = None

    # ------------------------------------------------------------- lifecycle

    def set_event_sink(self, sink: Callable[..., Any] | None) -> None:
        with self._lock:
            self._on_event = sink

    def _emit(self, event: str, **data: Any) -> None:
        with self._lock:
            sink = self._on_event
        if sink is None:
            return
        try:
            sink(event, **data)
        except Exception:
            # Instrumentation must never take the link down with it.
            pass

    def attach(
        self,
        *,
        send: Callable[[dict[str, Any]], None],
        loop: asyncio.AbstractEventLoop,
        user_agent: str = "",
        clock: Callable[[], float] = time.time,
    ) -> PhoneInfo:
        """Make this connection the phone, displacing any previous one."""

        info = PhoneInfo(
            connection_id=uuid.uuid4().hex,
            user_agent=user_agent[:200],
            connected_at=clock(),
        )
        with self._lock:
            previous = self._info
            self._info = info
            self._send = send
            self._loop = loop
            mic_open = self._mic_open
        if previous is not None:
            self._emit("phone.replaced", previous=previous.connection_id)
        self._emit("phone.attached", connection=info.connection_id)
        # A phone that connects mid-turn has to be told the microphone is
        # already wanted, or the turn listens to a stream nobody is filling.
        if mic_open:
            self._post({"type": "mic", "open": True})
        return info

    def detach(self, connection_id: str | None = None) -> bool:
        """Drop the phone.  A stale connection id is ignored, not obeyed."""

        with self._lock:
            info = self._info
            if info is None:
                return False
            if connection_id is not None and connection_id != info.connection_id:
                # A late close from the connection we already replaced.
                return False
            self._info = None
            self._send = None
            self._loop = None
            # A phone that left cannot still be where the user is speaking.
            self._chosen = False
            playbacks = tuple(self._playbacks.values())
            self._playbacks.clear()
        # Nothing will ever ack these now; releasing them is what keeps a turn
        # from hanging on a wav that went out to a phone in a tunnel.
        for playback in playbacks:
            playback.finished.set()
        self._emit("phone.detached", connection=info.connection_id)
        return True

    def choose(self, phone: bool) -> None:
        """Record which machine the user just spoke from.

        Connected is not chosen. A phone in a pocket, or an app left open on
        the desk, used to take the microphone and the speaker away from the
        Mac the user was sitting at — the voice went to whichever device
        happened to be attached, which is a fact about the network and not
        about where the person is. Ali's rule, in his words: *the voice
        follows you*. So attaching only makes the phone available, and
        speaking is what makes it the destination.
        """

        with self._lock:
            if self._chosen == phone:
                return
            self._chosen = phone
        self._emit("phone.chosen" if phone else "phone.released")

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._info is not None

    @property
    def is_destination(self) -> bool:
        """Should the microphone and the speaker be the phone's right now?

        Both halves have to be true: attached, and the last place the user
        spoke. Everything that used to ask `connected` asks this instead.
        """

        with self._lock:
            return self._info is not None and self._chosen

    @property
    def info(self) -> PhoneInfo | None:
        with self._lock:
            return self._info

    def status(self) -> dict[str, Any]:
        with self._lock:
            info = self._info
            mic_open = self._mic_open
            chosen = self._chosen
            playing = len(self._playbacks)
        if info is None:
            return {"connected": False}
        return {
            "connected": True,
            "connection_id": info.connection_id,
            "user_agent": info.user_agent,
            "connected_at": info.connected_at,
            "sample_rate": info.sample_rate,
            "mic_open": mic_open,
            "playbacks_in_flight": playing,
            # Whether this phone is merely reachable or is actually where the
            # voice goes — the distinction a connected-but-idle phone needs.
            "is_destination": chosen,
        }

    # ------------------------------------------------------------- transport

    def _post(self, message: dict[str, Any]) -> bool:
        """Hand one message to the loop that owns the socket."""

        with self._lock:
            send = self._send
            loop = self._loop
        if send is None or loop is None:
            return False
        try:
            loop.call_soon_threadsafe(send, message)
        except RuntimeError:
            # The loop is closing; the disconnect path will clean up.
            return False
        return True

    def notify(self, message: dict[str, Any]) -> bool:
        """Send one control message to the attached phone, if there is one.

        Playback and the microphone have their own methods because they own
        state here. This is for the messages that only report something the
        Mac knows and the phone cannot work out — chiefly that a whole spoken
        utterance is over, not just the span the phone last acked.
        """

        return self._post(dict(message))

    # --------------------------------------------------------------- capture

    def set_mic_open(self, open_: bool) -> None:
        """Tell the phone whether to send audio at all.

        This is the honest remote equivalent of the macOS microphone
        indicator: while it is false the phone stops the track, so there is no
        audio in flight to drop, buffer or leak — "deaf" means the far end is
        not recording, not that this end is discarding.
        """

        with self._lock:
            if self._mic_open == open_:
                return
            self._mic_open = open_
        self._post({"type": "mic", "open": open_})
        self._emit("phone.mic", open=open_)

    def subscribe(self) -> queue.Queue[np.ndarray]:
        frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_FRAMES)
        with self._lock:
            self._subscribers.add(frames)
        return frames

    def unsubscribe(self, frames: queue.Queue[np.ndarray]) -> None:
        with self._lock:
            self._subscribers.discard(frames)

    def push_pcm(self, payload: bytes) -> None:
        """Fan one chunk of int16 mono PCM out to every open capture."""

        if not payload:
            return
        samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        with self._lock:
            subscribers = tuple(self._subscribers)
        for frames in subscribers:
            try:
                frames.put_nowait(samples)
            except queue.Full:
                try:
                    frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    frames.put_nowait(samples)
                except queue.Full:
                    pass

    # -------------------------------------------------------------- playback

    def start_playback(self, wav_bytes: bytes, *, volume: float, duration_s: float) -> str:
        playback = _Playback(uuid.uuid4().hex, duration_s)
        with self._lock:
            if self._info is None:
                raise RemoteAudioUnavailable("no phone is attached")
            self._playbacks[playback.id] = playback
        delivered = self._post(
            {
                "type": "play",
                "id": playback.id,
                "volume": round(float(volume), 3),
                "wav": base64.b64encode(wav_bytes).decode("ascii"),
            }
        )
        if not delivered:
            self.finish_playback(playback.id)
            raise RemoteAudioUnavailable("the phone link closed before playback started")
        return playback.id

    def finish_playback(self, playback_id: str) -> None:
        with self._lock:
            playback = self._playbacks.pop(playback_id, None)
        if playback is not None:
            playback.finished.set()

    def cancel_playback(self, playback_id: str) -> None:
        with self._lock:
            playback = self._playbacks.get(playback_id)
            if playback is not None:
                playback.cancelled = True
        self._post({"type": "cancel", "id": playback_id})
        self.finish_playback(playback_id)

    def playback_running(self, playback_id: str) -> bool:
        with self._lock:
            playback = self._playbacks.get(playback_id)
        return playback is not None and not playback.finished.is_set()

    def wait_playback(self, playback_id: str, timeout: float | None = None) -> bool:
        with self._lock:
            playback = self._playbacks.get(playback_id)
        if playback is None:
            return True
        # The wav's own length is the floor: a phone that never acks (locked
        # screen, backgrounded tab) must not hold the turn open forever, and a
        # phone that acks late must not have its playback cut short.
        bound = playback.duration_s + PLAYBACK_ACK_GRACE_S
        if timeout is not None:
            bound = min(bound, timeout)
        return playback.finished.wait(timeout=bound)


PHONE = PhoneLink()


# --------------------------------------------------------------------- capture


class _RemoteDefaults:
    device = (0, 0)


class RemoteSoundDevice:
    """The shim ``resolve_input_device`` needs to describe the phone.

    It deliberately has no ``_terminate``/``_initialize``: ``refresh_devices``
    treats their absence as "nothing to refresh" and leaves the link alone,
    which is exactly right for a device that is a socket.
    """

    default = _RemoteDefaults()

    def query_devices(self, _device: Any = None, _kind: str | None = None) -> dict[str, Any]:
        info = PHONE.info
        name = "phone"
        if info is not None and info.user_agent:
            name = f"phone ({_short_agent(info.user_agent)})"
        return {
            "name": name,
            "default_samplerate": float(REMOTE_SAMPLE_RATE),
            "max_input_channels": 1,
            "max_output_channels": 2,
        }


def _short_agent(user_agent: str) -> str:
    lowered = user_agent.lower()
    for marker, label in (
        ("android", "Android"),
        ("iphone", "iPhone"),
        ("ipad", "iPad"),
        ("macintosh", "Mac"),
    ):
        if marker in lowered:
            return label
    return "browser"


class RemoteInputStream:
    """A ``sounddevice.InputStream`` whose hardware is a phone.

    It hands the capture callback frames of exactly ``blocksize`` samples, the
    same contract PortAudio keeps, so the endpointer downstream sees the
    stream it was tuned against.
    """

    def __init__(
        self,
        *,
        samplerate: int,
        channels: int = 1,
        dtype: str = "float32",
        blocksize: int = 320,
        callback: Callable[..., None] | None = None,
        device: Any = None,
        link: PhoneLink | None = None,
    ) -> None:
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.dtype = dtype
        self.blocksize = max(1, int(blocksize))
        self._callback = callback
        self._device = device
        self._link = link or PHONE
        self._frames: queue.Queue[np.ndarray] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._frames = self._link.subscribe()
        self._link.set_mic_open(True)
        thread = threading.Thread(target=self._pump, name="vox-remote-capture", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        frames = self._frames
        self._frames = None
        if frames is not None:
            self._link.unsubscribe(frames)
        self._link.set_mic_open(False)

    def close(self) -> None:
        self.stop()

    def _pump(self) -> None:
        frames = self._frames
        callback = self._callback
        if frames is None or callback is None:
            return
        pending = np.zeros(0, dtype=np.float32)
        while not self._stop.is_set():
            try:
                chunk = frames.get(timeout=0.1)
            except queue.Empty:
                continue
            pending = np.concatenate((pending, chunk)) if pending.size else chunk
            while pending.size >= self.blocksize:
                block = pending[: self.blocksize]
                pending = pending[self.blocksize :]
                try:
                    callback(block.reshape(-1, 1), self.blocksize, None, None)
                except Exception:
                    # The capture callback owns its own error reporting; a
                    # raise here would only kill the pump thread silently.
                    return


def remote_stream_factory(**kwargs: Any) -> RemoteInputStream:
    return RemoteInputStream(**kwargs)


# -------------------------------------------------------------------- playback


class RemotePlaybackProcess:
    """A phone playing a wav, wearing the shape of a player subprocess.

    ``PlaybackHandle`` only ever asks four things of what it is holding, so
    answering those four is enough to get cancellation, the registry and
    temp-file cleanup for free instead of forking a second playback path.
    """

    def __init__(self, playback_id: str, *, link: PhoneLink | None = None) -> None:
        self.pid = -1
        self.id = playback_id
        self._link = link or PHONE

    def poll(self) -> int | None:
        return None if self._link.playback_running(self.id) else 0

    def wait(self, timeout: float | None = None) -> int:
        if not self._link.wait_playback(self.id, timeout=timeout):
            # Match subprocess semantics so PlaybackHandle.cancel escalates.
            import subprocess

            raise subprocess.TimeoutExpired(cmd="vox-phone-playback", timeout=timeout or 0.0)
        return 0

    def terminate(self) -> None:
        self._link.cancel_playback(self.id)

    def kill(self) -> None:
        self._link.cancel_playback(self.id)


def wav_duration_s(payload: bytes) -> float:
    """Length of a PCM wav, or 0.0 if the header cannot be read."""

    import io
    import wave

    try:
        with wave.open(io.BytesIO(payload), "rb") as handle:
            rate = handle.getframerate()
            if rate <= 0:
                return 0.0
            return handle.getnframes() / float(rate)
    except Exception:
        return 0.0


__all__ = [
    "PHONE",
    "PLAYBACK_ACK_GRACE_S",
    "REMOTE_SAMPLE_RATE",
    "PhoneInfo",
    "PhoneLink",
    "RemoteAudioUnavailable",
    "RemoteInputStream",
    "RemotePlaybackProcess",
    "RemoteSoundDevice",
    "remote_stream_factory",
    "wav_duration_s",
]
