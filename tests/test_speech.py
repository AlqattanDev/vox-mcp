import json
from pathlib import Path

import httpx
import pytest

from voxmcp.errors import VoxError
from voxmcp.speech import LocalSpeechClient, api_url


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
async def test_transcribe_rejects_missing_file(tmp_path: Path):
    client = LocalSpeechClient(
        tts_base_url="http://127.0.0.1:8880/v1",
        stt_base_url="http://127.0.0.1:2022/v1",
    )
    with pytest.raises(VoxError, match="does not exist"):
        await client.transcribe(tmp_path / "missing.wav")
