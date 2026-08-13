from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tomllib
from typing import Any

import pytest

from voxmcp.installer import (
    LEGACY_BACKEND_LABELS,
    MCP_URL,
    STALE_LABELS,
    TRACKED_LABELS,
    VOX_LABELS,
    InstallerPaths,
    TransactionalInstaller,
    generate_launchd_plists,
    set_codex_vox_tool_timeout,
)


FIXED_NOW = datetime(2026, 7, 10, 12, 34, 56, 789000, tzinfo=timezone.utc)
UID = 501


class FakeRunner:
    """Launchctl/host CLI double; every call must remain argv-only."""

    def __init__(
        self,
        *,
        loaded: set[str] | None = None,
        fail_bootstrap: set[str] | None = None,
    ) -> None:
        self.loaded = set(loaded or set())
        self.fail_bootstrap = set(fail_bootstrap or set())
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        self.calls.append((command, kwargs))
        assert isinstance(argv, list)
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True

        if Path(command[0]).name == "launchctl":
            verb = command[1]
            if verb == "print":
                label = command[2].rsplit("/", 1)[-1]
                if label in self.loaded:
                    output = f"\tstate = running\n\tpid = 123\n\truns = 7\n"
                    return subprocess.CompletedProcess(argv, 0, output, "")
                return subprocess.CompletedProcess(argv, 113, "", "service not found")
            if verb == "bootout":
                label = command[2].rsplit("/", 1)[-1]
                self.loaded.discard(label)
                return subprocess.CompletedProcess(argv, 0, "", "")
            if verb == "bootstrap":
                label = Path(command[3]).stem
                if label in self.fail_bootstrap:
                    return subprocess.CompletedProcess(argv, 5, "", "simulated bootstrap failure")
                self.loaded.add(label)
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 64, "", "unsupported launchctl verb")

        # Claude and Codex registration are tested as planned argv calls. The
        # fake intentionally does not touch real host state or user configs.
        return subprocess.CompletedProcess(argv, 0, "", "")


def write_executable(path: Path, content: bytes = b"#!/bin/sh\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)


def make_paths(tmp_path: Path) -> InstallerPaths:
    home = tmp_path / "home"
    project = tmp_path / "project"
    vox_root = home / ".vox"
    launch_agents = home / "Library" / "LaunchAgents"
    app_source = project / "dist" / "Vox.app"
    app_binary = app_source / "Contents" / "MacOS" / "VoxStatus"
    write_executable(app_binary, b"fake signed app")
    (app_source / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "local.vox.mcp.status"})
    )

    runtime = project / ".venv" / "bin" / "voxd"
    whisper_root = home / ".voicemode" / "services" / "whisper"
    whisper_binary = whisper_root / "build" / "bin" / "whisper-server"
    whisper_model = whisper_root / "models" / "ggml-large-v3-turbo.bin"
    kokoro_root = home / ".voicemode" / "services" / "kokoro"
    kokoro_python = kokoro_root / ".venv" / "bin" / "python"
    claude_cli = tmp_path / "bin" / "claude"
    codex_cli = tmp_path / "bin" / "codex"
    launchctl = tmp_path / "bin" / "launchctl"
    for executable in (runtime, whisper_binary, kokoro_python, claude_cli, codex_cli, launchctl):
        write_executable(executable)
    whisper_model.parent.mkdir(parents=True, exist_ok=True)
    whisper_model.write_bytes(b"whisper model")
    (kokoro_root / "api").mkdir(parents=True, exist_ok=True)

    skill = project / "skills" / "vox"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Vox\nUse local voice.\n", encoding="utf-8")

    claude_config = home / ".claude.json"
    codex_config = home / ".codex" / "config.toml"
    claude_config.parent.mkdir(parents=True, exist_ok=True)
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    claude_config.write_text('{"mcpServers":{"old":{}}}\n', encoding="utf-8")
    codex_config.write_text(
        '[mcp_servers.vox]\nurl = "http://127.0.0.1:8766/mcp"\n'
        "tool_timeout_sec = 120\n\n[projects.demo]\ntrusted = true\n",
        encoding="utf-8",
    )

    return InstallerPaths(
        home=home,
        project_root=project,
        vox_root=vox_root,
        rollback_root=vox_root / "rollback",
        launch_agents=launch_agents,
        app_source=app_source,
        app_target=home / "Applications" / "Vox.app",
        runtime_executable=runtime,
        whisper_root=whisper_root,
        whisper_binary=whisper_binary,
        whisper_model=whisper_model,
        kokoro_root=kokoro_root,
        kokoro_python=kokoro_python,
        skill_source=skill,
        claude_skill_target=home / ".claude" / "skills" / "vox",
        codex_skill_target=home / ".codex" / "skills" / "vox",
        claude_config=claude_config,
        codex_config=codex_config,
        claude_cli=claude_cli,
        codex_cli=codex_cli,
        launchctl=launchctl,
    )


def make_installer(
    paths: InstallerPaths,
    runner: FakeRunner | None = None,
    health_checker=None,
) -> TransactionalInstaller:
    return TransactionalInstaller(
        paths,
        runner=runner or FakeRunner(),
        uid=UID,
        now=lambda: FIXED_NOW,
        health_checker=health_checker,
    )


def test_plists_use_direct_absolute_loopback_only_commands(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    rendered = generate_launchd_plists(paths)
    assert set(rendered) == set(VOX_LABELS)

    runtime = plistlib.loads(rendered["com.vox.runtime"])
    assert runtime["ProgramArguments"] == [
        str(paths.app_target / "Contents" / "MacOS" / "VoxStatus")
    ]
    assert runtime["EnvironmentVariables"]["VOX_RUNTIME"] == str(paths.runtime_executable)
    assert Path(runtime["EnvironmentVariables"]["VOX_RUNTIME"]).is_absolute()
    assert runtime["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")
    assert runtime["KeepAlive"] == {"SuccessfulExit": False}

    whisper = plistlib.loads(rendered["com.vox.whisper"])
    assert whisper["ProgramArguments"][0] == str(paths.whisper_binary)
    assert whisper["ProgramArguments"][whisper["ProgramArguments"].index("--host") + 1] == "127.0.0.1"
    assert str(paths.whisper_model) in whisper["ProgramArguments"]
    assert whisper["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")

    kokoro = plistlib.loads(rendered["com.vox.kokoro"])
    assert kokoro["ProgramArguments"][:4] == [
        str(paths.kokoro_python),
        "-m",
        "uvicorn",
        "api.src.main:app",
    ]
    assert kokoro["ProgramArguments"][kokoro["ProgramArguments"].index("--host") + 1] == "127.0.0.1"

    all_text = b"\n".join(rendered.values()).decode().lower()
    for forbidden in (
        "0.0.0.0",
        "uv pip",
        "uvx",
        "download_model",
        "limit-max-requests",
        "max_requests",
    ):
        assert forbidden not in all_text


def test_dry_run_is_side_effect_free_and_lists_host_registration(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    runner = FakeRunner()
    installer = make_installer(paths, runner)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    plan = installer.install(dry_run=True)

    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert before == after
    assert runner.calls == []
    assert plan.safe_to_activate is True
    assert plan.directories == ()
    assert plan.unload_labels == STALE_LABELS + LEGACY_BACKEND_LABELS
    assert plan.delete_targets == tuple(
        paths.plist_for_label(label)
        for label in STALE_LABELS + LEGACY_BACKEND_LABELS
    )
    assert plan.to_dict()["delete_targets"] == [
        str(path) for path in plan.delete_targets
    ]
    assert plan.load_labels == VOX_LABELS
    commands = [command.argv for command in plan.host_commands]
    assert (
        str(paths.claude_cli),
        "mcp",
        "add",
        "--transport",
        "http",
        "--scope",
        "user",
        "vox",
        MCP_URL,
    ) in commands
    assert (str(paths.codex_cli), "mcp", "add", "vox", "--url", MCP_URL) in commands
    assert (
        str(paths.claude_cli),
        "mcp",
        "remove",
        "--scope",
        "user",
        "voicemode",
    ) in commands
    assert (str(paths.codex_cli), "mcp", "remove", "voicemode") in commands
    assert paths.claude_skill_target in plan.backup_targets
    assert paths.codex_skill_target in plan.backup_targets
    assert paths.claude_config in plan.backup_targets
    assert paths.codex_config in plan.backup_targets
    assert plan.to_dict()["dry_run"] is True


def test_plan_reports_missing_prerequisites_and_activation_stays_explicit(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    paths.runtime_executable.unlink()
    installer = make_installer(paths)
    plan = installer.build_plan()

    assert plan.safe_to_activate is False
    assert any("VOX_RUNTIME is missing" in issue for issue in plan.issues)
    with pytest.raises(PermissionError, match="confirm=True"):
        installer.activate(plan)
    with pytest.raises(ValueError, match="unsafe plan"):
        installer.activate(plan, confirm=True)


def test_codex_timeout_updates_only_dedicated_vox_section() -> None:
    original = (
        "tool_timeout_sec = 9\n"
        "[mcp_servers.other]\n"
        'url = "http://127.0.0.1:9000/mcp"\n'
        "tool_timeout_sec = 30\n\n"
        '[mcp_servers."vox"] # voice\n'
        'url = "http://127.0.0.1:8766/mcp"\n'
        "tool_timeout_sec = 60 # old\n\n"
        "[projects.demo]\n"
        "trusted = true\n"
    )

    updated = set_codex_vox_tool_timeout(original, 600)
    parsed = tomllib.loads(updated)

    assert parsed["tool_timeout_sec"] == 9
    assert parsed["mcp_servers"]["other"]["tool_timeout_sec"] == 30
    assert parsed["mcp_servers"]["vox"]["tool_timeout_sec"] == 600
    assert parsed["projects"]["demo"]["trusted"] is True
    assert updated.count("tool_timeout_sec = 600") == 1


def test_codex_timeout_adds_vox_section_but_refuses_inline_shape() -> None:
    added = set_codex_vox_tool_timeout("model = \"gpt-5\"\n", 600)
    assert tomllib.loads(added)["mcp_servers"]["vox"]["tool_timeout_sec"] == 600

    inline = '[mcp_servers]\nvox = { url = "http://127.0.0.1:8766/mcp" }\n'
    with pytest.raises(ValueError, match="dedicated"):
        set_codex_vox_tool_timeout(inline, 600)


def test_backup_manifest_contains_hashes_and_loaded_states(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    legacy_plist = paths.plist_for_label("com.voicemode.whisper")
    legacy_plist.parent.mkdir(parents=True, exist_ok=True)
    legacy_plist.write_text("legacy whisper plist\n", encoding="utf-8")
    runner = FakeRunner(loaded={"com.voicemode.whisper", "com.voicemode.connect"})
    installer = make_installer(paths, runner)

    backup = installer.create_backup(installer.build_plan())
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert backup.parent == paths.rollback_root
    assert backup.name == "20260710T123456.789000Z"
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert manifest["activated"] is False
    assert set(manifest["loaded_states"]) == set(TRACKED_LABELS)
    assert manifest["loaded_states"]["com.voicemode.whisper"]["loaded"] is True
    assert manifest["loaded_states"]["com.voicemode.connect"]["loaded"] is True
    assert manifest["loaded_states"]["com.vox.runtime"]["loaded"] is False

    claude_entry = next(
        entry for entry in manifest["entries"] if entry["path"] == str(paths.claude_config)
    )
    assert claude_entry["kind"] == "file"
    assert claude_entry["sha256"] == hashlib.sha256(paths.claude_config.read_bytes()).hexdigest()
    assert (backup / claude_entry["backup"]).is_file()
    assert all(kwargs["shell"] is False for _, kwargs in runner.calls)


def test_explicit_rollback_restores_files_and_previously_loaded_jobs(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    old_claude = paths.claude_config.read_bytes()
    shared_skill = paths.home / "shared-skill"
    shared_skill.mkdir(parents=True)
    paths.claude_skill_target.parent.mkdir(parents=True, exist_ok=True)
    paths.claude_skill_target.symlink_to(shared_skill, target_is_directory=True)
    legacy_labels = {"com.voicemode.whisper", "com.voicemode.kokoro"}
    for label in legacy_labels:
        plist = paths.plist_for_label(label)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"{label} original\n", encoding="utf-8")
    runner = FakeRunner(loaded=set(legacy_labels))
    installer = make_installer(paths, runner)
    plan = installer.build_plan()
    backup = installer.create_backup(plan)

    paths.claude_config.write_text("mutated\n", encoding="utf-8")
    paths.runtime_plist.parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_plist.write_text("new runtime\n", encoding="utf-8")
    paths.claude_skill_target.unlink()
    paths.claude_skill_target.mkdir()
    (paths.claude_skill_target / "wrong.md").write_text("wrong", encoding="utf-8")
    runner.loaded.difference_update(legacy_labels)
    runner.loaded.update(VOX_LABELS)

    with pytest.raises(PermissionError, match="confirm=True"):
        installer.rollback(backup)
    result = installer.rollback(backup, confirm=True)

    assert result.success is True
    assert paths.claude_config.read_bytes() == old_claude
    assert not paths.runtime_plist.exists()
    assert paths.claude_skill_target.is_symlink()
    assert os.readlink(paths.claude_skill_target) == str(shared_skill)
    assert runner.loaded == legacy_labels
    assert paths.claude_config in result.restored_paths
    assert all(kwargs["shell"] is False for _, kwargs in runner.calls)


def test_tampered_backup_is_rejected_before_overwriting_target(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    runner = FakeRunner()
    installer = make_installer(paths, runner)
    backup = installer.create_backup(installer.build_plan())
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["entries"] if item["path"] == str(paths.claude_config))
    (backup / entry["backup"]).write_text("tampered", encoding="utf-8")
    paths.claude_config.write_text("current must survive\n", encoding="utf-8")
    calls_before = len(runner.calls)

    result = installer.rollback(backup, confirm=True)

    assert result.success is False
    assert "hash mismatch" in (result.error or "")
    assert paths.claude_config.read_text(encoding="utf-8") == "current must survive\n"
    assert len(runner.calls) == calls_before


def test_fake_activation_failure_automatically_rolls_back_everything(
    tmp_path: Path,
) -> None:
    """This activates only against a temporary home and injected fake runner."""

    paths = make_paths(tmp_path)
    original_claude = paths.claude_config.read_bytes()
    original_codex = paths.codex_config.read_bytes()
    originally_loaded = set(STALE_LABELS + LEGACY_BACKEND_LABELS)
    for label in originally_loaded:
        plist = paths.plist_for_label(label)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"{label} original\n", encoding="utf-8")
    runner = FakeRunner(
        loaded=set(originally_loaded),
        fail_bootstrap={"com.vox.kokoro"},
    )
    installer = make_installer(paths, runner)

    result = installer.activate(installer.build_plan(), confirm=True)

    assert result.success is False
    assert result.rolled_back is True
    assert "simulated bootstrap failure" in (result.error or "")
    assert paths.claude_config.read_bytes() == original_claude
    assert paths.codex_config.read_bytes() == original_codex
    assert not paths.runtime_plist.exists()
    assert not paths.whisper_plist.exists()
    assert not paths.kokoro_plist.exists()
    assert not paths.app_target.exists()
    assert not paths.claude_skill_target.exists()
    assert not paths.codex_skill_target.exists()
    assert installer.build_plan().directories == ()
    assert runner.loaded == originally_loaded
    assert all(kwargs["shell"] is False for _, kwargs in runner.calls)


def test_failed_backend_health_canary_automatically_rolls_back(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    original_claude = paths.claude_config.read_bytes()
    originally_loaded = set(LEGACY_BACKEND_LABELS)
    for label in originally_loaded:
        plist = paths.plist_for_label(label)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"{label} original\n", encoding="utf-8")
    checked: list[tuple[str, float]] = []

    def health_checker(url: str, timeout: float) -> bool:
        checked.append((url, timeout))
        return "8880" not in url

    runner = FakeRunner(loaded=set(originally_loaded))
    installer = make_installer(paths, runner, health_checker=health_checker)

    result = installer.activate(installer.build_plan(), confirm=True)

    assert result.success is False
    assert result.rolled_back is True
    assert "Kokoro failed its loopback health canary" in (result.error or "")
    assert checked == [
        ("http://127.0.0.1:2022/health", 90.0),
        ("http://127.0.0.1:8880/health", 120.0),
    ]
    assert paths.claude_config.read_bytes() == original_claude
    assert runner.loaded == originally_loaded


def test_installer_paths_reject_targets_outside_home(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    values = {name: getattr(paths, name) for name in paths.__dataclass_fields__}
    values["app_target"] = tmp_path / "outside" / "Vox.app"
    with pytest.raises(ValueError, match="inside"):
        InstallerPaths(**values)


def test_every_delete_target_is_backed_up_and_spares_current_vox_plists(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    plan = make_installer(paths).build_plan()

    # The rollback-safety precondition: nothing is deleted that was not first
    # captured in the backup snapshot.
    assert set(plan.delete_targets) <= set(plan.backup_targets)
    assert not set(plan.delete_targets) & {
        paths.runtime_plist,
        paths.whisper_plist,
        paths.kokoro_plist,
    }


def test_successful_activation_deletes_legacy_plists_loaded_or_not(
    tmp_path: Path,
) -> None:
    """A bootout leaves the plist behind; RunAtLoad then revives it at login."""

    paths = make_paths(tmp_path)
    loaded_label = "com.voicemode.whisper"
    unloaded_label = "com.voicemode.kokoro"
    for label in (loaded_label, unloaded_label):
        plist = paths.plist_for_label(label)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"{label} original\n", encoding="utf-8")
    # Only one of the two is loaded; the other exists on disk untouched by launchd.
    runner = FakeRunner(loaded={loaded_label})
    installer = make_installer(paths, runner, health_checker=lambda url, timeout: True)

    result = installer.activate(installer.build_plan(), confirm=True)

    assert result.success is True
    assert result.rolled_back is False
    assert not paths.plist_for_label(loaded_label).exists()
    assert not paths.plist_for_label(unloaded_label).exists()

    # The unloaded plist must be deleted without ever being booted out. If the
    # deletion is ever re-nested under the loaded_states gate, only this fails.
    booted_out = [
        command[2].rsplit("/", 1)[-1]
        for command, _ in runner.calls
        if len(command) > 2 and command[1] == "bootout"
    ]
    assert loaded_label in booted_out
    assert unloaded_label not in booted_out

    # The plists we just installed are not collateral damage.
    assert paths.runtime_plist.is_file()
    assert paths.whisper_plist.is_file()
    assert paths.kokoro_plist.is_file()
    assert runner.loaded == set(VOX_LABELS)

    manifest = json.loads((result.backup_dir / "manifest.json").read_text())
    assert manifest["activated"] is True
    assert all(kwargs["shell"] is False for _, kwargs in runner.calls)


def test_activation_failure_restores_deleted_legacy_plists(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    loaded_label = "com.voicemode.whisper"
    unloaded_label = "com.voicemode.kokoro"
    originals = {}
    for label in (loaded_label, unloaded_label):
        plist = paths.plist_for_label(label)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"{label} original\n", encoding="utf-8")
        originals[label] = plist.read_bytes()
    # Fail the last bootstrap, so the failure lands after the deletion loop.
    runner = FakeRunner(loaded={loaded_label}, fail_bootstrap={"com.vox.runtime"})
    installer = make_installer(paths, runner, health_checker=lambda url, timeout: True)

    result = installer.activate(installer.build_plan(), confirm=True)

    assert result.success is False
    assert result.rolled_back is True
    for label, original in originals.items():
        assert paths.plist_for_label(label).read_bytes() == original
    # The loaded one is bootstrapped again from the restored file; restoring the
    # bytes must happen before launchd is asked to load them back.
    assert runner.loaded == {loaded_label}
    assert all(kwargs["shell"] is False for _, kwargs in runner.calls)
