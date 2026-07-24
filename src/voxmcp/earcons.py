"""Short local audio cues that mark exactly when the microphone opens and closes.

Vox never recorded a "you may talk now" / "I stopped listening" signal, so the
user could not tell a live mic from a dead one — the loudest complaint about the
runtime. These two synthesized tones close that gap. They are generated once,
cached under the Vox home, and played through the normal cancellable player.

The start tone plays *blocking, before* the capture stream opens, so it can never
leak into the recording; the stop tone plays fire-and-forget once the mic closes.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from .audio import write_wav_atomic

# Bump when the tones change so cached wavs regenerate on the next run.
_VERSION = "v1"
_SAMPLE_RATE = 24_000
_AMPLITUDE = 0.28


def earcons_enabled(environ: dict[str, str] | None = None) -> bool:
    """Earcons are on unless explicitly disabled via ``VOX_EARCONS``."""

    env = environ if environ is not None else os.environ
    return env.get("VOX_EARCONS", "1").strip().lower() not in {"0", "false", "no", "off"}


def _segment(freq: float, duration_s: float) -> np.ndarray:
    count = max(1, round(_SAMPLE_RATE * duration_s))
    t = np.arange(count) / _SAMPLE_RATE
    wave = np.sin(2.0 * np.pi * freq * t).astype(np.float32)
    # 5 ms attack/release so the tone edges don't click.
    edge = max(1, round(_SAMPLE_RATE * 0.005))
    envelope = np.ones(count, dtype=np.float32)
    if count >= 2 * edge:
        envelope[:edge] = np.linspace(0.0, 1.0, edge, dtype=np.float32)
        envelope[-edge:] = np.linspace(1.0, 0.0, edge, dtype=np.float32)
    return wave * envelope


def _tone(sequence: list[tuple[float, float]]) -> np.ndarray:
    return np.concatenate([_segment(f, d) for f, d in sequence]) * _AMPLITUDE


# Rising two-note blip = "mic open, talk now"; falling = "mic closed, I stopped".
_START_SEQUENCE = [(660.0, 0.075), (990.0, 0.085)]
_STOP_SEQUENCE = [(880.0, 0.075), (560.0, 0.100)]


def ensure_earcons(assets_dir: Path) -> tuple[Path, Path]:
    """Return ``(start_wav, stop_wav)``, synthesizing them if not already cached."""

    assets_dir = Path(assets_dir).expanduser()
    start = assets_dir / f"listen_start.{_VERSION}.wav"
    stop = assets_dir / f"listen_stop.{_VERSION}.wav"
    if not start.is_file():
        write_wav_atomic(start, _tone(_START_SEQUENCE), _SAMPLE_RATE)
    if not stop.is_file():
        write_wav_atomic(stop, _tone(_STOP_SEQUENCE), _SAMPLE_RATE)
    return start, stop
