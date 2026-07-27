"""Strictly local speech-to-text and text-to-speech clients.

The servers speak an OpenAI-compatible wire format, but this module never
imports credentials, never follows redirects, ignores proxy environment
variables, and accepts only URLs already validated as loopback by VoxConfig.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .errors import ServiceUnavailableError, VoxError


@dataclass(slots=True)
class SpeechResult:
    backend: str
    elapsed_ms: int
    attempts: int = 1
    path: str | None = None
    text: str | None = None
    fallback: bool = False
    language: str | None = None
    duration: float | None = None
    segments: list[dict[str, Any]] | None = None
    words: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def api_url(base_url: str, suffix: str) -> str:
    """Join a versioned local base URL and an API suffix without duplication."""
    parts = urlsplit(base_url)
    base_path = parts.path.rstrip("/")
    suffix_path = "/" + suffix.strip("/")
    if suffix_path.startswith("/v1/") and base_path.endswith("/v1"):
        suffix_path = suffix_path[3:]
    path = base_path + suffix_path
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class LocalSpeechClient:
    """Calls loopback Kokoro and Whisper endpoints with bounded recovery."""

    def __init__(
        self,
        *,
        tts_base_url: str,
        stt_base_url: str,
        ensure_service: Callable[[str], Awaitable[object]] | None = None,
        timeout_tts: float = 60.0,
        timeout_stt: float = 180.0,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        whisper_cli: Path | None = None,
        whisper_model: Path | None = None,
    ) -> None:
        self.tts_base_url = tts_base_url
        self.stt_base_url = stt_base_url
        self.ensure_service = ensure_service
        self.timeout_tts = timeout_tts
        self.timeout_stt = timeout_stt
        self.client_factory = client_factory
        self.whisper_cli = whisper_cli
        self.whisper_model = whisper_model

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return self.client_factory(
            timeout=httpx.Timeout(timeout, connect=3.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def _recover(self, service: str) -> None:
        if self.ensure_service is not None:
            try:
                await self.ensure_service(service)
            except Exception:
                # Recovery is best-effort; the bounded second request and
                # eventual typed error remain deterministic.
                pass

    async def synthesize(
        self,
        text: str,
        destination: Path,
        *,
        voice: str = "af_sky",
        speed: float = 1.0,
        model: str = "tts-1",
        instructions: str | None = None,
    ) -> SpeechResult:
        if not text.strip():
            raise VoxError("Cannot synthesize an empty message")
        if not 0.25 <= speed <= 4.0:
            raise VoxError("TTS speed must be between 0.25 and 4.0")

        destination.parent.mkdir(parents=True, exist_ok=True)
        endpoint = api_url(self.tts_base_url, "audio/speech")
        payload: dict[str, object] = {
            "model": model,
            "input": text,
            "voice": voice.lower(),
            "speed": speed,
            "response_format": "wav",
        }
        if instructions:
            payload["instructions"] = instructions

        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in (1, 2):
            temp_path: Path | None = None
            try:
                fd, raw_path = tempfile.mkstemp(
                    prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
                )
                os.close(fd)
                temp_path = Path(raw_path)
                async with self._client(self.timeout_tts) as client:
                    async with client.stream("POST", endpoint, json=payload) as response:
                        response.raise_for_status()
                        with temp_path.open("wb") as output:
                            async for chunk in response.aiter_bytes():
                                output.write(chunk)
                if temp_path.stat().st_size < 44:
                    raise ServiceUnavailableError("Kokoro returned an empty audio response")
                os.replace(temp_path, destination)
                return SpeechResult(
                    backend="kokoro",
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    attempts=attempt,
                    path=str(destination),
                )
            except (httpx.HTTPError, OSError, ServiceUnavailableError) as exc:
                last_error = exc
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                if attempt == 1:
                    await self._recover("kokoro")
                    continue
        raise ServiceUnavailableError(f"Local Kokoro TTS failed after recovery: {last_error}")

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = "en",
        prompt: str | None = None,
        model: str = "whisper-1",
        word_timestamps: bool = False,
    ) -> SpeechResult:
        if not audio_path.is_file():
            raise VoxError(f"Audio file does not exist: {audio_path}")
        endpoint = api_url(self.stt_base_url, "audio/transcriptions")
        started = time.monotonic()
        last_error: Exception | None = None

        for attempt in (1, 2):
            try:
                data: dict[str, str] = {
                    "model": model,
                    "response_format": "verbose_json" if word_timestamps else "json",
                }
                if word_timestamps:
                    data["word_timestamps"] = "true"
                if language:
                    data["language"] = language
                if prompt:
                    data["prompt"] = prompt
                async with self._client(self.timeout_stt) as client:
                    with audio_path.open("rb") as audio:
                        response = await client.post(
                            endpoint,
                            data=data,
                            files={"file": (audio_path.name, audio, "audio/wav")},
                        )
                    response.raise_for_status()
                    payload = response.json()
                text = payload.get("text") if isinstance(payload, dict) else None
                if not isinstance(text, str):
                    raise ServiceUnavailableError("Whisper response did not contain text")
                return SpeechResult(
                    backend="whisper.cpp",
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    attempts=attempt,
                    text=text.strip(),
                    language=(
                        str(payload.get("language"))
                        if payload.get("language") is not None
                        else language
                    ),
                    duration=(
                        float(payload["duration"])
                        if payload.get("duration") is not None
                        else None
                    ),
                    segments=(
                        payload.get("segments")
                        if isinstance(payload.get("segments"), list)
                        else None
                    ),
                    words=(
                        payload.get("words")
                        if isinstance(payload.get("words"), list)
                        else None
                    ),
                )
            except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError, ServiceUnavailableError) as exc:
                last_error = exc
                if attempt == 1:
                    await self._recover("whisper")
                    continue

        fallback = await self._transcribe_with_cli(audio_path, language=language)
        if fallback is not None:
            fallback.elapsed_ms = round((time.monotonic() - started) * 1000)
            return fallback
        raise ServiceUnavailableError(f"Local Whisper STT failed after recovery: {last_error}")

    async def _transcribe_with_cli(
        self, audio_path: Path, *, language: str | None
    ) -> SpeechResult | None:
        cli = self.whisper_cli
        model = self.whisper_model
        if cli is None or model is None or not cli.is_file() or not model.is_file():
            return None

        with tempfile.TemporaryDirectory(prefix="vox-whisper-") as raw_dir:
            output_base = Path(raw_dir) / "transcript"
            command = [
                str(cli),
                "-m",
                str(model),
                "-f",
                str(audio_path),
                "-otxt",
                "-of",
                str(output_base),
                "-nt",
            ]
            if language:
                command.extend(["-l", language])
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_stt,
                check=False,
            )
            transcript_path = output_base.with_suffix(".txt")
            if completed.returncode != 0 or not transcript_path.is_file():
                return None
            return SpeechResult(
                backend="whisper-cli",
                elapsed_ms=0,
                text=transcript_path.read_text(errors="replace").strip(),
                fallback=True,
            )


async def synthesize_with_macos_say(
    text: str,
    destination: Path,
    *,
    voice: str | None = None,
    rate: int | None = None,
    timeout: float = 120.0,
) -> SpeechResult:
    """Zero-network TTS fallback using the built-in macOS speech synthesizer."""
    say = shutil.which("say")
    ffmpeg = shutil.which("ffmpeg")
    if say is None or ffmpeg is None:
        raise ServiceUnavailableError("macOS say/ffmpeg fallback is unavailable")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vox-say-") as raw_dir:
        aiff = Path(raw_dir) / "speech.aiff"
        command = [say, "-o", str(aiff)]
        if voice:
            command.extend(["-v", voice])
        if rate:
            command.extend(["-r", str(rate)])
        command.append(text)
        started = time.monotonic()
        say_run = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if say_run.returncode != 0:
            raise ServiceUnavailableError("macOS say failed")
        temp_wav = destination.with_suffix(destination.suffix + ".part")
        convert = await asyncio.to_thread(
            subprocess.run,
            [
                ffmpeg,
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(aiff),
                "-f",
                "wav",
                str(temp_wav),
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if convert.returncode != 0:
            temp_wav.unlink(missing_ok=True)
            raise ServiceUnavailableError("Could not convert macOS say output")
        os.replace(temp_wav, destination)
        return SpeechResult(
            backend="macOS say",
            elapsed_ms=round((time.monotonic() - started) * 1000),
            path=str(destination),
            fallback=True,
        )
