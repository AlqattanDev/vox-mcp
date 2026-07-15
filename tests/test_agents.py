from pathlib import Path

import pytest

from voxmcp.agents import AgentVoices, FALLBACK_VOICES
from voxmcp.engine import VoxEngine
from tests.test_engine import make_engine


POOL = ["af_sky", "af_heart", "af_bella", "am_adam", "am_michael", "bf_emma", "bm_george"]


def test_default_agent_keeps_the_default_voice(tmp_path: Path):
    voices = AgentVoices(tmp_path / "agents.json", default_voice="af_sky")
    assert voices.resolve("default", POOL) == "af_sky"
    # Nothing is persisted for single-agent use.
    assert not (tmp_path / "agents.json").exists()


def test_configured_mapping_is_honoured(tmp_path: Path):
    path = tmp_path / "agents.json"
    path.write_text('{"infra": "am_michael", "mobilescape": "af_bella"}')
    voices = AgentVoices(path, default_voice="af_sky")
    assert voices.resolve("infra", POOL) == "am_michael"
    assert voices.resolve("mobilescape", POOL) == "af_bella"


def test_distinct_agents_get_distinct_voices(tmp_path: Path):
    voices = AgentVoices(tmp_path / "agents.json", default_voice="af_sky")
    assigned = {label: voices.resolve(label, POOL) for label in ("infra", "mobilescape", "vox")}
    assert len(set(assigned.values())) == 3  # no two agents sound alike
    assert "af_sky" not in assigned.values()  # nor collide with the default


def test_assignment_survives_a_restart(tmp_path: Path):
    path = tmp_path / "agents.json"
    first = AgentVoices(path, default_voice="af_sky")
    original = first.resolve("infra", POOL)

    # A fresh process reading the same file must agree.
    second = AgentVoices(path, default_voice="af_sky")
    assert second.resolve("infra", POOL) == original
    assert second.assignments["infra"] == original


def test_assignment_is_hashed_not_insertion_ordered(tmp_path: Path):
    """A label's voice must not depend on who registered first."""

    forwards = AgentVoices(tmp_path / "a.json", default_voice="af_sky")
    for label in ("alpha", "infra"):
        forwards.resolve(label, POOL)

    backwards = AgentVoices(tmp_path / "b.json", default_voice="af_sky")
    for label in ("infra", "alpha"):
        backwards.resolve(label, POOL)

    assert forwards.assignments["infra"] == backwards.assignments["infra"]


def test_more_agents_than_voices_still_resolves(tmp_path: Path):
    voices = AgentVoices(tmp_path / "agents.json", default_voice="af_sky")
    pool = ["af_sky", "af_bella"]
    resolved = [voices.resolve(f"agent{index}", pool) for index in range(5)]
    assert all(voice in pool for voice in resolved)


def test_unreadable_map_falls_back_to_assignment(tmp_path: Path):
    path = tmp_path / "agents.json"
    path.write_text("{ this is not json")
    voices = AgentVoices(path, default_voice="af_sky")
    assert voices.resolve("infra", POOL) in POOL


def test_fallback_pool_is_used_when_no_voices_are_offered(tmp_path: Path):
    voices = AgentVoices(tmp_path / "agents.json", default_voice="af_sky")
    assert voices.resolve("infra", None) in FALLBACK_VOICES


@pytest.mark.asyncio
async def test_engine_speaks_each_agent_in_its_own_voice(tmp_path: Path):
    engine = make_engine(tmp_path)
    engine._voice_pool = list(POOL)
    spoken: list[str] = []

    original = engine._speak_locked

    async def record(client_id, message, *, voice, **kwargs):
        spoken.append(voice)
        return await original(client_id, message, voice=voice, **kwargs)

    engine._speak_locked = record

    await engine.speak("claude", "from infra", agent="infra")
    await engine.speak("claude", "from mobilescape", agent="mobilescape")
    await engine.speak("claude", "unlabelled")

    infra, mobilescape, default = spoken
    assert infra != mobilescape  # the voice is the signal
    assert default == engine.default_voice  # single-agent use is untouched


@pytest.mark.asyncio
async def test_explicit_voice_beats_the_agent_voice(tmp_path: Path):
    engine = make_engine(tmp_path)
    engine._voice_pool = list(POOL)
    spoken: list[str] = []

    original = engine._speak_locked

    async def record(client_id, message, *, voice, **kwargs):
        spoken.append(voice)
        return await original(client_id, message, voice=voice, **kwargs)

    engine._speak_locked = record

    await engine.speak("claude", "override", agent="infra", voice="bm_george")
    assert spoken == ["bm_george"]


@pytest.mark.asyncio
async def test_engine_voice_is_stable_across_a_restart(tmp_path: Path):
    engine = make_engine(tmp_path)
    engine._voice_pool = list(POOL)
    first = await engine._agent_voice("infra")

    # A new engine over the same home is what a daemon restart looks like.
    restarted = make_engine(tmp_path)
    restarted._voice_pool = list(POOL)
    assert await restarted._agent_voice("infra") == first


@pytest.mark.asyncio
async def test_session_status_reports_the_callers_voice(tmp_path: Path):
    engine = make_engine(tmp_path)
    engine._voice_pool = list(POOL)

    status = await engine.session("claude", "status", agent="infra")
    assert status["agent"]["id"] == "infra"
    assert status["agent"]["voice"] == await engine._agent_voice("infra")

    default_status = await engine.session("claude", "status")
    assert default_status["agent"]["id"] == "default"
    assert default_status["agent"]["voice"] == engine.default_voice


@pytest.mark.asyncio
async def test_voice_registry_surfaces_the_agent_mapping(tmp_path: Path):
    engine = make_engine(tmp_path)
    engine._voice_pool = list(POOL)
    assigned = await engine._agent_voice("infra")

    registry = await engine.voice_registry("claude")
    assert registry["agents"]["infra"] == assigned
    assert registry["default"] == engine.default_voice
