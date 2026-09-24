import json
from pathlib import Path

import pytest

from voxmcp.agents import AgentVoices, FALLBACK_VOICES, is_english_voice
from voxmcp.engine import VoxEngine
from tests.test_engine import make_engine


POOL = ["af_sky", "af_heart", "af_bella", "am_adam", "am_michael", "bf_emma", "bm_george"]

# The shape Kokoro actually serves: 9 languages, plus superseded v0 variants.
REAL_POOL = [
    "af_bella", "af_heart", "af_sky", "af_v0", "af_jessica", "af_nicole",
    "am_adam", "am_michael", "am_fenrir", "am_echo",
    "bf_emma", "bf_alice", "bf_v0emma",
    "bm_george", "bm_fable", "bm_v0george",
    "ef_dora", "em_alex", "ff_siwis", "hf_alpha", "hm_omega", "if_sara",
    "im_nicola", "jf_nezumi", "jf_alpha", "jm_kumo", "pf_dora", "pm_alex",
    "zf_xiaobei", "zf_xiaoni", "zm_yunxia", "zm_yunjian",
]


def test_only_english_voices_are_auto_assignable():
    assert is_english_voice("am_michael") is True
    assert is_english_voice("bf_emma") is True
    # The bug: an agent read English status updates in a Mandarin voice.
    assert is_english_voice("zm_yunxia") is False
    assert is_english_voice("jf_nezumi") is False
    assert is_english_voice("hm_omega") is False
    # Superseded duplicates of the same speaker.
    assert is_english_voice("af_v0") is False
    assert is_english_voice("bm_v0george") is False


def test_auto_assignment_never_picks_a_foreign_voice(tmp_path: Path):
    voices = AgentVoices(tmp_path / "agents.json", default_voice="af_sky")
    assigned = [
        voices.resolve(f"project-{index}", REAL_POOL) for index in range(30)
    ]
    assert all(is_english_voice(voice) for voice in assigned), assigned


def test_explicit_mapping_may_still_choose_any_voice(tmp_path: Path):
    """A voice the user wrote down by hand is their call, not ours."""

    path = tmp_path / "agents.json"
    path.write_text('{"tokyo": "jf_nezumi"}')
    voices = AgentVoices(path, default_voice="af_sky")
    assert voices.resolve("tokyo", REAL_POOL) == "jf_nezumi"


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


def test_serialized_voice_objects_are_repaired_and_persisted(tmp_path: Path):
    """A UI-shaped voice must not become a literal, nonexistent Kokoro id."""

    path = tmp_path / "agents.json"
    path.write_text(json.dumps({
        "codex": "{'id': 'bm_daniel', 'name': 'bm_daniel'}",
        "claude": {"id": "af_bella", "name": "Bella"},
    }))

    voices = AgentVoices(path, default_voice="af_sky")

    assert voices.assignments == {"codex": "bm_daniel", "claude": "af_bella"}
    assert json.loads(path.read_text()) == {
        "claude": "af_bella",
        "codex": "bm_daniel",
    }


def test_malformed_voice_id_is_discarded_and_reassigned(tmp_path: Path):
    path = tmp_path / "agents.json"
    path.write_text('{"codex": "not a Kokoro voice"}')
    voices = AgentVoices(path, default_voice="af_sky")

    assert voices.resolve("codex", POOL) in POOL
    assert voices.resolve("codex", POOL) != "not a Kokoro voice"


def test_new_assignment_normalizes_object_shaped_voice_list(tmp_path: Path):
    """The first turn must work even when the voices endpoint returns objects."""

    voices = AgentVoices(tmp_path / "agents.json", default_voice="af_sky")
    offered = [
        "{'id': 'bm_daniel', 'name': 'Daniel'}",
        '{"id": "af_bella", "name": "Bella"}',
    ]

    assert voices.resolve("codex", offered) in {"bm_daniel", "af_bella"}
    assert set(voices.assignments.values()) <= {"bm_daniel", "af_bella"}


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
