import io
import subprocess
import wave
from pathlib import Path

import httpx
import pytest

from voxmcp.errors import ServiceUnavailableError, VoxError
from voxmcp.speech import LocalSpeechClient, api_url, synthesize_with_macos_say


def _wav_bytes(*, duration_s: float, sample_rate: int = 22_050) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\0\0" * round(duration_s * sample_rate))
    return output.getvalue()


def test_api_url_avoids_duplicate_v1():
    assert (
        api_url("http://127.0.0.1:8880/v1", "/v1/audio/speech")
        == "http://127.0.0.1:8880/v1/audio/speech"
    )
    assert (
        api_url("http://127.0.0.1:8880/v1", "audio/speech")
        == "http://127.0.0.1:8880/v1/audio/speech"
    )


@pytest.mark.asyncio
async def test_synthesize_rejects_empty_message(tmp_path: Path):
    client = LocalSpeechClient(
        tts_base_url="http://127.0.0.1:8880/v1",
        stt_base_url="http://127.0.0.1:2022/v1",
    )
    with pytest.raises(VoxError, match="empty"):
        await client.synthesize("  ", tmp_path / "out.wav")


@pytest.mark.asyncio
async def test_synthesize_rejects_a_header_only_success_response(tmp_path: Path):
    """HTTP 200 is not speech when the WAV is only a few milliseconds long."""

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=_wav_bytes(duration_s=0.005))
    )
    client = LocalSpeechClient(
        tts_base_url="http://127.0.0.1:8880/v1",
        stt_base_url="http://127.0.0.1:2022/v1",
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )

    with pytest.raises(ServiceUnavailableError, match="too short"):
        await client.synthesize("This should be audible.", tmp_path / "out.wav")


@pytest.mark.asyncio
async def test_macos_say_rejects_an_effectively_empty_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """say(1) can exit zero while producing the silent 5 ms file seen live."""

    monkeypatch.setattr("voxmcp.speech.shutil.which", lambda name: f"/usr/bin/{name}")

    def run(command, **_kwargs):
        if Path(command[0]).name == "say":
            Path(command[2]).write_bytes(b"AIFF")
        else:
            Path(command[-1]).write_bytes(_wav_bytes(duration_s=0.005))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("voxmcp.speech.subprocess.run", run)

    with pytest.raises(ServiceUnavailableError, match="too short"):
        await synthesize_with_macos_say("This should be audible.", tmp_path / "out.wav")


@pytest.mark.asyncio
async def test_transcribe_rejects_missing_file(tmp_path: Path):
    client = LocalSpeechClient(
        tts_base_url="http://127.0.0.1:8880/v1",
        stt_base_url="http://127.0.0.1:2022/v1",
    )
    with pytest.raises(VoxError, match="does not exist"):
        await client.transcribe(tmp_path / "missing.wav")


def test_a_header_that_claims_audio_it_does_not_contain_is_refused(tmp_path: Path):
    from voxmcp.speech import _validate_synthesized_wav

    wav = bytearray(_wav_bytes(duration_s=1.0))[:44]
    path = tmp_path / "empty.wav"
    path.write_bytes(bytes(wav))

    with pytest.raises(ServiceUnavailableError, match="too short"):
        _validate_synthesized_wav(path)
