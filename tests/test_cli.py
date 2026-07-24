from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest
from click.testing import CliRunner

from voxmcp.cli import (
    DEFAULT_READ_TIMEOUT_SECONDS,
    MCPToolCaller,
    create_cli,
)


class FakeCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        copied = dict(arguments)
        self.calls.append((tool_name, copied))
        return {"tool": tool_name, "arguments": copied, "ok": True}


@dataclass
class FakeResult:
    kind: str
    values: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.values}


class FakeInstaller:
    def __init__(self) -> None:
        self.install_calls: list[dict[str, Any]] = []
        self.rollback_calls: list[tuple[Path, bool]] = []

    def install(self, **kwargs: Any) -> FakeResult:
        self.install_calls.append(kwargs)
        return FakeResult("install", kwargs)

    def rollback(self, backup_dir: Path, *, confirm: bool) -> FakeResult:
        self.rollback_calls.append((backup_dir, confirm))
        return FakeResult("rollback", {"backup_dir": backup_dir, "confirm": confirm})


def invoke(
    arguments: list[str],
    *,
    caller: FakeCaller | None = None,
    installer: FakeInstaller | None = None,
    runtime_starter: Any | None = None,
):
    selected_caller = caller or FakeCaller()
    selected_installer = installer or FakeInstaller()
    command = create_cli(
        mcp_caller=selected_caller,
        installer=selected_installer,
        runtime_starter=runtime_starter,
    )
    result = CliRunner().invoke(command, arguments)
    return result, selected_caller, selected_installer


def output_json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_help_lists_the_complete_command_surface() -> None:
    result, _caller, _installer = invoke(["--help"])

    assert result.exit_code == 0
    for command in (
        "status",
        "session",
        "speak",
        "listen",
        "converse",
        "control",
        "doctor",
        "service",
        "transcribe",
        "voices",
        "dj",
        "clone",
        "soundfonts",
        "install",
        "rollback",
        "start-runtime",
    ):
        assert command in result.output


@pytest.mark.parametrize(
    ("argv", "tool", "arguments"),
    [
        (["status"], "voice_session", {"action": "status"}),
        (["session", "start"], "voice_session", {"action": "start"}),
        (["session", "stop"], "voice_session", {"action": "stop"}),
        (
            ["session", "pause", "--seconds", "12"],
            "voice_session",
            {"action": "pause", "seconds": 12.0},
        ),
        (["session", "resume"], "voice_session", {"action": "resume"}),
        (["control", "cancel"], "voice_control", {"action": "cancel"}),
        (["control", "manual-end"], "voice_control", {"action": "end_turn"}),
        (["control", "repeat"], "voice_control", {"action": "repeat"}),
    ],
)
def test_session_and_control_commands_map_directly_to_mcp(
    argv: list[str], tool: str, arguments: dict[str, Any]
) -> None:
    caller = FakeCaller()
    result, _caller, _installer = invoke(argv, caller=caller)

    payload = output_json(result)
    assert caller.calls == [(tool, arguments)]
    assert payload == {"arguments": arguments, "ok": True, "tool": tool}


def test_speak_listen_and_converse_preserve_explicit_options() -> None:
    caller = FakeCaller()

    speak, _caller, _installer = invoke(
        ["speak", "Working now", "--voice", "af_sky", "--speed", "1.25"],
        caller=caller,
    )
    output_json(speak)
    assert caller.calls[-1] == (
        "speak",
        {
            "message": "Working now",
            "voice": "af_sky",
            "speed": 1.25,
            "interruptible": True,
        },
    )

    listen, _caller, _installer = invoke(
        [
            "listen",
            "--listen-duration-max",
            "240",
            "--listen-duration-min",
            "2",
            "--trailing-silence-s",
            "1.8",
            "--language",
            "en",
        ],
        caller=caller,
    )
    output_json(listen)
    assert caller.calls[-1] == (
        "listen",
        {
            "listen_duration_max": 240.0,
            "listen_duration_min": 2.0,
            "trailing_silence_s": 1.8,
            "language": "en",
        },
    )

    converse, _caller, _installer = invoke(
        ["converse", "No reply needed", "--no-wait", "--voice", "af_river"],
        caller=caller,
    )
    output_json(converse)
    assert caller.calls[-1] == (
        "converse",
        {
            "message": "No reply needed",
            "wait_for_response": False,
            "voice": "af_river",
            "listen_duration_max": 75.0,
            "listen_duration_min": 0.5,
            "trailing_silence_s": 1.6,
        },
    )


def test_diagnostics_services_and_compatibility_tools_are_mcp_calls(
    tmp_path: Path,
) -> None:
    caller = FakeCaller()
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF")

    invocations = [
        (["doctor", "--include-logs"], "diagnostics", {"section": "all", "include_logs": True}),
        (
            ["service", "logs", "whisper", "--lines", "25"],
            "service",
            {"action": "logs", "service_name": "whisper", "lines": 25},
        ),
        (["voices", "--provider", "kokoro"], "voice_registry", {"provider": "kokoro"}),
        (
            ["dj", "play", "/tmp/local.mp3", "--volume", "35"],
            "dj",
            {"action": "play", "target": "/tmp/local.mp3", "volume": 35},
        ),
        (["soundfonts", "off"], "soundfonts", {"action": "off"}),
        (
            ["clone", "show", "river"],
            "voice_clone",
            {"action": "show", "name": "river"},
        ),
        (
            ["transcribe", str(audio), "--language", "en", "--word-timestamps"],
            "transcribe",
            {
                "audio_file": str(audio.resolve()),
                "language": "en",
                "output_format": "text",
                "word_timestamps": True,
            },
        ),
    ]

    for argv, tool, arguments in invocations:
        result, _caller, _installer = invoke(argv, caller=caller)
        output_json(result)
        assert caller.calls[-1] == (tool, arguments)


def test_install_is_side_effect_free_by_default() -> None:
    installer = FakeInstaller()

    result, _caller, _installer = invoke(["install"], installer=installer)

    payload = output_json(result)
    assert installer.install_calls == [
        {"dry_run": True, "confirm_activation": False}
    ]
    assert payload["dry_run"] is True
    assert payload["confirm_activation"] is False


@pytest.mark.parametrize("argv", [["install", "--activate"], ["install", "--yes"]])
def test_install_activation_requires_both_explicit_flags(argv: list[str]) -> None:
    installer = FakeInstaller()

    result, _caller, _installer = invoke(argv, installer=installer)

    assert result.exit_code == 2
    assert "requires both --activate and --yes" in result.output
    assert installer.install_calls == []


def test_explicit_activation_passes_both_installer_guards() -> None:
    installer = FakeInstaller()

    result, _caller, _installer = invoke(
        ["install", "--activate", "--yes"], installer=installer
    )

    payload = output_json(result)
    assert installer.install_calls == [
        {"dry_run": False, "confirm_activation": True}
    ]
    assert payload["dry_run"] is False
    assert payload["confirm_activation"] is True


def test_rollback_requires_confirmation_and_uses_exact_backup(tmp_path: Path) -> None:
    backup = tmp_path / "rollback-001"
    backup.mkdir()
    installer = FakeInstaller()

    denied, _caller, _installer = invoke(
        ["rollback", str(backup)], installer=installer
    )
    assert denied.exit_code == 2
    assert installer.rollback_calls == []

    accepted, _caller, _installer = invoke(
        ["rollback", str(backup), "--yes"], installer=installer
    )
    payload = output_json(accepted)
    assert installer.rollback_calls == [(backup.resolve(), True)]
    assert payload["backup_dir"] == str(backup.resolve())


def test_start_runtime_uses_only_injected_installed_runtime_boundary() -> None:
    installer = FakeInstaller()
    calls: list[tuple[Any, float]] = []

    def starter(selected_installer: Any, wait_seconds: float) -> dict[str, Any]:
        calls.append((selected_installer, wait_seconds))
        return {"started": True, "source": "installed-runtime"}

    result, _caller, _installer = invoke(
        ["start-runtime", "--wait-seconds", "8"],
        installer=installer,
        runtime_starter=starter,
    )

    assert output_json(result)["source"] == "installed-runtime"
    assert calls == [(installer, 8.0)]
    assert installer.install_calls == []


def test_runtime_call_failures_are_actionable_click_errors() -> None:
    def broken(_tool: str, _arguments: Mapping[str, Any]) -> Any:
        raise ConnectionError("connection refused")

    command = create_cli(mcp_caller=broken, installer=FakeInstaller())
    result = CliRunner().invoke(command, ["status"])

    assert result.exit_code == 1
    assert "connection refused" in result.output
    assert "vox start-runtime" in result.output


def test_mcp_caller_is_fixed_to_loopback_and_has_a_generous_timeout() -> None:
    caller = MCPToolCaller()
    assert caller.url == "http://127.0.0.1:8766/mcp"
    assert caller.read_timeout_seconds == DEFAULT_READ_TIMEOUT_SECONDS
    assert caller.read_timeout_seconds >= 600

    with pytest.raises(ValueError):
        MCPToolCaller(url="https://example.com/mcp")
