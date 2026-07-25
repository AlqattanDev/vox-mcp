from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from voxmcp.config import ConfigurationError, VoxConfig, validate_loopback_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:2022/v1/", "http://localhost:2022/v1"),
        ("HTTP://127.0.0.1:8880/v1", "http://127.0.0.1:8880/v1"),
        ("http://127.42.7.9:9000", "http://127.42.7.9:9000"),
        ("https://[::1]:8890/v1", "https://[::1]:8890/v1"),
    ],
)
def test_loopback_urls_are_normalized(value: str, expected: str) -> None:
    assert validate_loopback_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://api.openai.com/v1",
        "http://localhost.example:2022/v1",
        "http://0.0.0.0:2022/v1",
        "http://192.168.1.20:2022/v1",
        "http://10.0.0.2:2022/v1",
        "http://2130706433:2022/v1",
        "ftp://127.0.0.1/v1",
        "http://user:pass@127.0.0.1:2022/v1",
        "http://127.0.0.1:2022/v1?redirect=cloud",
        "http://127.0.0.1:2022/v1#fragment",
        "not-a-url",
        "",
    ],
)
def test_non_loopback_or_ambiguous_urls_are_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_loopback_url(value)


def test_safe_config_defaults_are_local_and_private(tmp_path: Path) -> None:
    config = VoxConfig(state_dir=tmp_path)

    assert config.stt_url == "http://127.0.0.1:2022/v1"
    assert config.tts_url == "http://127.0.0.1:8880/v1"
    assert config.local_only is True
    assert config.privacy_enabled is True
    assert config.persist_audio is True
    assert config.persist_transcripts is False
    assert config.tool_timeout_seconds == 600.0
    assert config.snapshot_path == tmp_path / "state.json"
    assert config.event_log_path == tmp_path / "events.jsonl"


def test_environment_loader_is_strict(tmp_path: Path) -> None:
    config = VoxConfig.from_env(
        {
            "VOX_STATE_DIR": str(tmp_path),
            "VOX_STT_URL": "http://[::1]:2022/v1/",
            "VOX_TTS_URL": "http://localhost:8880/v1",
            "VOX_IDLE_TIMEOUT_SECONDS": "45",
            "VOX_TOOL_TIMEOUT_SECONDS": "360",
            "VOX_PRIVACY_ENABLED": "yes",
            "VOX_PERSIST_TRANSCRIPTS": "false",
        }
    )

    assert config.stt_url == "http://[::1]:2022/v1"
    assert config.idle_timeout_seconds == 45
    assert config.tool_timeout_seconds == 360

    with pytest.raises(ConfigurationError):
        VoxConfig.from_env({"VOX_PRIVACY_ENABLED": "perhaps"})
    with pytest.raises(ConfigurationError):
        VoxConfig.from_env({"VOX_STT_URL": "https://api.openai.com/v1"})


def test_barge_in_is_off_until_explicitly_enabled(tmp_path: Path) -> None:
    # Arming barge-in opens the microphone while the agent is still speaking,
    # so it must never be the default anyone gets by accident.
    assert VoxConfig(state_dir=tmp_path).barge_in_enabled is False
    assert VoxConfig.from_env({"VOX_STATE_DIR": str(tmp_path)}).barge_in_enabled is False

    enabled = VoxConfig.from_env({"VOX_STATE_DIR": str(tmp_path), "VOX_BARGE_IN_ENABLED": "1"})
    assert enabled.barge_in_enabled is True

    with pytest.raises(ConfigurationError):
        VoxConfig.from_env({"VOX_BARGE_IN_ENABLED": "sometimes"})


def test_config_rejects_unsafe_paths_and_ranges(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        VoxConfig(state_dir=tmp_path, event_log_filename="../events.jsonl")
    with pytest.raises(ConfigurationError):
        VoxConfig(state_dir=tmp_path, idle_timeout_seconds=-1)
    with pytest.raises(ConfigurationError):
        VoxConfig(state_dir=tmp_path, tool_timeout_seconds=0)
    with pytest.raises(ConfigurationError):
        VoxConfig(state_dir=tmp_path, local_only=False)


def test_settings_file_reaches_a_runtime_launchd_started(tmp_path: Path, monkeypatch) -> None:
    # launchd hands the runtime none of the shell's environment and does not
    # reliably pass `launchctl setenv`, so every VOX_* knob was unreachable in
    # the installed deployment. The file is the configuration that arrives.
    from voxmcp.config import load_user_settings

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "VOX_COMPANION_ENABLED": "1",
        "VOX_BARGE_IN_ENABLED": True,
        "VOX_TRAILING_SILENCE_SECONDS": 1.2,
        "PATH": "/nope",            # not VOX_-prefixed; must be ignored
    }))
    monkeypatch.delenv("VOX_COMPANION_ENABLED", raising=False)
    monkeypatch.delenv("VOX_BARGE_IN_ENABLED", raising=False)
    monkeypatch.delenv("VOX_TRAILING_SILENCE_SECONDS", raising=False)
    original_path = os.environ.get("PATH")

    applied = load_user_settings(settings)

    assert applied["VOX_COMPANION_ENABLED"] == "1"
    assert os.environ["VOX_BARGE_IN_ENABLED"] == "1"       # JSON true -> "1"
    assert os.environ["VOX_TRAILING_SILENCE_SECONDS"] == "1.2"
    # Never inject arbitrary names into the process environment.
    assert "PATH" not in applied
    assert os.environ.get("PATH") == original_path
    assert VoxConfig.from_env().companion_enabled is True


def test_a_real_environment_variable_still_wins(tmp_path: Path, monkeypatch) -> None:
    # So a one-off `VOX_BARGE_IN_ENABLED=1 voxd` overrides the file for one run.
    from voxmcp.config import load_user_settings

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"VOX_COMPANION_ENABLED": "0"}))
    monkeypatch.setenv("VOX_COMPANION_ENABLED", "1")

    load_user_settings(settings)

    assert os.environ["VOX_COMPANION_ENABLED"] == "1"


def test_a_missing_settings_file_is_not_an_error(tmp_path: Path) -> None:
    from voxmcp.config import load_user_settings

    assert load_user_settings(tmp_path / "absent.json") == {}


def test_unreadable_settings_fail_loudly(tmp_path: Path) -> None:
    # A typo'd settings file must not silently mean "defaults".
    from voxmcp.config import load_user_settings

    broken = tmp_path / "settings.json"
    broken.write_text("{not json")
    with pytest.raises(ConfigurationError):
        load_user_settings(broken)
