"""Transactional macOS installer and VoiceMode migration for Vox.

Planning is side-effect free.  Activation is explicit, creates a complete
rollback snapshot before its first mutation, and uses only argv-based host and
launchctl commands.  Backend launch agents execute already-installed binaries
directly: installing packages, downloading models, resolving updates, and
sourcing shell configuration are deliberately outside the runtime path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from typing import Any, Callable, Literal, Mapping, Sequence
import urllib.error
import urllib.request
import uuid


MCP_URL = "http://127.0.0.1:8766/mcp"
MANIFEST_VERSION = 1
VOX_LABELS = ("com.vox.runtime", "com.vox.whisper", "com.vox.kokoro")
STALE_LABELS = (
    "com.voicemode.connect",
    "com.voicemode.whisper-keepalive",
)
LEGACY_BACKEND_LABELS = (
    "com.voicemode.whisper",
    "com.voicemode.kokoro",
)
TRACKED_LABELS = VOX_LABELS + STALE_LABELS + LEGACY_BACKEND_LABELS


Runner = Callable[..., subprocess.CompletedProcess[str]]
HealthChecker = Callable[[str, float], bool]


@dataclass(frozen=True, slots=True)
class InstallerPaths:
    """Absolute paths used by one installation transaction."""

    home: Path
    project_root: Path
    vox_root: Path
    rollback_root: Path
    launch_agents: Path
    app_source: Path
    app_target: Path
    runtime_executable: Path
    whisper_root: Path
    whisper_binary: Path
    whisper_model: Path
    kokoro_root: Path
    kokoro_python: Path
    skill_source: Path
    claude_skill_target: Path
    codex_skill_target: Path
    claude_config: Path
    codex_config: Path
    claude_cli: Path
    codex_cli: Path
    launchctl: Path = Path("/bin/launchctl")

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if isinstance(value, Path) and not value.is_absolute():
                raise ValueError(f"{field_name} must be absolute: {value}")
        _require_under_home(self.vox_root, self.home, "vox_root")
        _require_under_home(self.rollback_root, self.home, "rollback_root")
        for target_name in (
            "app_target",
            "claude_skill_target",
            "codex_skill_target",
            "claude_config",
            "codex_config",
            "launch_agents",
        ):
            _require_under_home(getattr(self, target_name), self.home, target_name)

    @classmethod
    def discover(
        cls,
        project_root: Path | None = None,
        home: Path | None = None,
    ) -> "InstallerPaths":
        project = (
            Path(__file__).resolve().parents[2]
            if project_root is None
            else Path(project_root).expanduser().resolve()
        )
        package_assets = Path(__file__).resolve().parent / "assets"
        source_layout = (project / "dist" / "Vox.app").is_dir()
        asset_root = project if source_layout else package_assets
        user_home = Path.home().resolve() if home is None else Path(home).expanduser().resolve()
        vox_root = user_home / ".vox"
        launch_agents = user_home / "Library" / "LaunchAgents"
        whisper_root = user_home / ".voicemode" / "services" / "whisper"
        kokoro_root = user_home / ".voicemode" / "services" / "kokoro"
        return cls(
            home=user_home,
            project_root=project,
            vox_root=vox_root,
            rollback_root=vox_root / "rollback",
            launch_agents=launch_agents,
            app_source=(
                asset_root / "dist" / "Vox.app"
                if source_layout
                else asset_root / "Vox.app"
            ),
            app_target=user_home / "Applications" / "Vox.app",
            runtime_executable=Path(sys.executable).absolute().with_name("voxd"),
            whisper_root=whisper_root,
            whisper_binary=whisper_root / "build" / "bin" / "whisper-server",
            whisper_model=whisper_root / "models" / "ggml-small.bin",
            kokoro_root=kokoro_root,
            kokoro_python=kokoro_root / ".venv" / "bin" / "python",
            skill_source=(
                asset_root / "skills" / "vox"
                if source_layout
                else asset_root / "skills" / "vox"
            ),
            claude_skill_target=user_home / ".claude" / "skills" / "vox",
            codex_skill_target=user_home / ".codex" / "skills" / "vox",
            claude_config=user_home / ".claude.json",
            codex_config=user_home / ".codex" / "config.toml",
            claude_cli=_which_absolute("claude"),
            codex_cli=_which_absolute("codex"),
        )

    @property
    def runtime_plist(self) -> Path:
        return self.launch_agents / "com.vox.runtime.plist"

    @property
    def whisper_plist(self) -> Path:
        return self.launch_agents / "com.vox.whisper.plist"

    @property
    def kokoro_plist(self) -> Path:
        return self.launch_agents / "com.vox.kokoro.plist"

    def plist_for_label(self, label: str) -> Path:
        known = {
            "com.vox.runtime": self.runtime_plist,
            "com.vox.whisper": self.whisper_plist,
            "com.vox.kokoro": self.kokoro_plist,
            "com.voicemode.connect": self.launch_agents / "com.voicemode.connect.plist",
            "com.voicemode.whisper-keepalive": self.launch_agents
            / "com.voicemode.whisper-keepalive.plist",
            "com.voicemode.whisper": self.launch_agents / "com.voicemode.whisper.plist",
            "com.voicemode.kokoro": self.launch_agents / "com.voicemode.kokoro.plist",
        }
        try:
            return known[label]
        except KeyError as exc:
            raise KeyError(f"unknown launchd label {label!r}") from exc


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """An atomic byte write or atomic directory replacement."""

    description: str
    target: Path
    kind: Literal["bytes", "tree"]
    content: bytes | None = None
    source: Path | None = None
    mode: int = 0o644
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "target": str(self.target),
            "kind": self.kind,
            "source": str(self.source) if self.source else None,
            "mode": oct(self.mode),
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PlannedCommand:
    """An argv-only external command shown in the dry-run plan."""

    description: str
    argv: tuple[str, ...]
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "argv": list(self.argv),
            "optional": self.optional,
        }


@dataclass(frozen=True, slots=True)
class InstallationPlan:
    """Side-effect-free migration plan suitable for CLI dry-run output."""

    generated_at: str
    directories: tuple[Path, ...]
    files: tuple[PlannedFile, ...]
    host_commands: tuple[PlannedCommand, ...]
    unload_labels: tuple[str, ...]
    load_labels: tuple[str, ...]
    backup_targets: tuple[Path, ...]
    issues: tuple[str, ...]

    @property
    def safe_to_activate(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": True,
            "generated_at": self.generated_at,
            "safe_to_activate": self.safe_to_activate,
            "issues": list(self.issues),
            "directories": [str(path) for path in self.directories],
            "files": [item.to_dict() for item in self.files],
            "host_commands": [command.to_dict() for command in self.host_commands],
            "unload_labels": list(self.unload_labels),
            "load_labels": list(self.load_labels),
            "backup_targets": [str(path) for path in self.backup_targets],
        }


@dataclass(frozen=True, slots=True)
class CommandExecution:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class InstallationResult:
    success: bool
    backup_dir: Path | None
    rolled_back: bool
    commands: tuple[CommandExecution, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "backup_dir": str(self.backup_dir) if self.backup_dir else None,
            "rolled_back": self.rolled_back,
            "commands": [command.to_dict() for command in self.commands],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class RollbackResult:
    success: bool
    backup_dir: Path
    restored_paths: tuple[Path, ...]
    commands: tuple[CommandExecution, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "backup_dir": str(self.backup_dir),
            "restored_paths": [str(path) for path in self.restored_paths],
            "commands": [command.to_dict() for command in self.commands],
            "error": self.error,
        }


def generate_launchd_plists(
    paths: InstallerPaths,
    *,
    whisper_threads: int = 12,
) -> Mapping[str, bytes]:
    """Generate direct, loopback-only launchd jobs with no mutable startup work."""

    if whisper_threads < 1:
        raise ValueError("whisper_threads must be positive")
    app_executable = paths.app_target / "Contents" / "MacOS" / "VoxStatus"

    runtime = {
        "Label": "com.vox.runtime",
        "ProgramArguments": [str(app_executable)],
        "EnvironmentVariables": {
            "VOX_RUNTIME": str(paths.runtime_executable),
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        },
        "RunAtLoad": True,
        # Normal menu-bar Quit exits 0 and stays stopped. Crashes exit nonzero
        # and launchd relaunches them.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        # Operational state is exposed by /health and redacted JSONL events.
        # Discard process streams so launchd cannot create another unbounded,
        # transcript-bearing log like the old Connect/Kokoro jobs did.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    whisper = {
        "Label": "com.vox.whisper",
        "ProgramArguments": [
            str(paths.whisper_binary),
            "--host",
            "127.0.0.1",
            "--port",
            "2022",
            "--model",
            str(paths.whisper_model),
            "--inference-path",
            "/v1/audio/transcriptions",
            "--threads",
            str(whisper_threads),
            "--convert",
        ],
        "WorkingDirectory": str(paths.whisper_root),
        "EnvironmentVariables": {
            # launchd does not inherit the interactive Homebrew PATH. Whisper's
            # local --convert path invokes ffmpeg by name for non-WAV inputs.
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    kokoro = {
        "Label": "com.vox.kokoro",
        "ProgramArguments": [
            str(paths.kokoro_python),
            "-m",
            "uvicorn",
            "api.src.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8880",
        ],
        "WorkingDirectory": str(paths.kokoro_root),
        "EnvironmentVariables": {
            "USE_GPU": "true",
            "USE_ONNX": "false",
            "PYTHONPATH": f"{paths.kokoro_root}:{paths.kokoro_root / 'api'}",
            "MODEL_DIR": "src/models",
            "VOICES_DIR": "src/voices/v1_0",
            "WEB_PLAYER_PATH": str(paths.kokoro_root / "web"),
            "DEVICE_TYPE": "mps",
            "PYTORCH_ENABLE_MPS_FALLBACK": "1",
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }

    documents = {
        "com.vox.runtime": runtime,
        "com.vox.whisper": whisper,
        "com.vox.kokoro": kokoro,
    }
    for label, document in documents.items():
        _validate_launchd_document(label, document)
    return {
        label: plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False)
        for label, document in documents.items()
    }


class TransactionalInstaller:
    """Plan, activate, and explicitly roll back one local macOS migration."""

    def __init__(
        self,
        paths: InstallerPaths | None = None,
        *,
        runner: Runner = subprocess.run,
        uid: int | None = None,
        now: Callable[[], datetime] | None = None,
        command_timeout: float = 30.0,
        health_checker: HealthChecker | None = None,
    ) -> None:
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        self.paths = InstallerPaths.discover() if paths is None else paths
        self._runner = runner
        self._uid = os.getuid() if uid is None else uid
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._command_timeout = command_timeout
        self._health_checker = health_checker or _wait_for_http_health

    @property
    def _domain(self) -> str:
        return f"gui/{self._uid}"

    def build_plan(self) -> InstallationPlan:
        """Return a complete dry-run plan without writing files or running commands."""

        plists = generate_launchd_plists(self.paths)
        files = (
            PlannedFile(
                "Install signed Vox.app",
                self.paths.app_target,
                "tree",
                source=self.paths.app_source,
                mode=0o755,
                sha256=_hash_path_if_present(self.paths.app_source),
            ),
            PlannedFile(
                "Install Claude Vox skill",
                self.paths.claude_skill_target,
                "tree",
                source=self.paths.skill_source,
                mode=0o755,
                sha256=_hash_path_if_present(self.paths.skill_source),
            ),
            PlannedFile(
                "Install Codex Vox skill",
                self.paths.codex_skill_target,
                "tree",
                source=self.paths.skill_source,
                mode=0o755,
                sha256=_hash_path_if_present(self.paths.skill_source),
            ),
            PlannedFile(
                "Install Vox runtime LaunchAgent",
                self.paths.runtime_plist,
                "bytes",
                content=plists["com.vox.runtime"],
                sha256=_sha256(plists["com.vox.runtime"]),
            ),
            PlannedFile(
                "Install direct Whisper LaunchAgent",
                self.paths.whisper_plist,
                "bytes",
                content=plists["com.vox.whisper"],
                sha256=_sha256(plists["com.vox.whisper"]),
            ),
            PlannedFile(
                "Install direct Kokoro LaunchAgent",
                self.paths.kokoro_plist,
                "bytes",
                content=plists["com.vox.kokoro"],
                sha256=_sha256(plists["com.vox.kokoro"]),
            ),
        )
        host_commands = (
            PlannedCommand(
                "Remove an existing Claude user-scope Vox registration if present",
                (str(self.paths.claude_cli), "mcp", "remove", "--scope", "user", "vox"),
                optional=True,
            ),
            PlannedCommand(
                "Register Vox HTTP MCP for Claude user scope",
                (
                    str(self.paths.claude_cli),
                    "mcp",
                    "add",
                    "--transport",
                    "http",
                    "--scope",
                    "user",
                    "vox",
                    MCP_URL,
                ),
            ),
            PlannedCommand(
                "Remove the replaced VoiceMode Claude registration if present",
                (
                    str(self.paths.claude_cli),
                    "mcp",
                    "remove",
                    "--scope",
                    "user",
                    "voicemode",
                ),
                optional=True,
            ),
            PlannedCommand(
                "Remove an existing Codex Vox registration if present",
                (str(self.paths.codex_cli), "mcp", "remove", "vox"),
                optional=True,
            ),
            PlannedCommand(
                "Register Vox HTTP MCP for Codex",
                (str(self.paths.codex_cli), "mcp", "add", "vox", "--url", MCP_URL),
            ),
            PlannedCommand(
                "Remove a replaced VoiceMode Codex registration if present",
                (str(self.paths.codex_cli), "mcp", "remove", "voicemode"),
                optional=True,
            ),
        )
        backup_targets = _dedupe_paths(
            tuple(item.target for item in files)
            + (
                self.paths.claude_config,
                self.paths.codex_config,
            )
            + tuple(self.paths.plist_for_label(label) for label in TRACKED_LABELS)
        )
        return InstallationPlan(
            generated_at=_iso(self._now()),
            directories=tuple(
                self.paths.vox_root / "logs" / service
                for service in ("runtime", "whisper", "kokoro")
            ),
            files=files,
            host_commands=host_commands,
            unload_labels=STALE_LABELS + LEGACY_BACKEND_LABELS,
            load_labels=VOX_LABELS,
            backup_targets=backup_targets,
            issues=tuple(self._validation_issues()),
        )

    def install(
        self,
        *,
        dry_run: bool = True,
        confirm_activation: bool = False,
    ) -> InstallationPlan | InstallationResult:
        """Plan by default; mutate only with two explicit activation flags."""

        plan = self.build_plan()
        if dry_run:
            return plan
        return self.activate(plan, confirm=confirm_activation)

    def create_backup(self, plan: InstallationPlan) -> Path:
        """Seal files, hashes, and pre-mutation launchd states into one snapshot."""

        timestamp = _timestamp(self._now())
        self.paths.rollback_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        final = self.paths.rollback_root / timestamp
        if os.path.lexists(final):
            raise FileExistsError(f"rollback snapshot already exists: {final}")
        stage = self.paths.rollback_root / f".{timestamp}.tmp-{uuid.uuid4().hex}"
        stage.mkdir(mode=0o700)
        try:
            entries = [
                self._backup_one(path, stage, index)
                for index, path in enumerate(plan.backup_targets)
            ]
            loaded_states = {
                label: self._capture_loaded_state(label) for label in TRACKED_LABELS
            }
            manifest = {
                "manifest_version": MANIFEST_VERSION,
                "created_at": _iso(self._now()),
                "home": str(self.paths.home),
                "activated": False,
                "entries": entries,
                "loaded_states": loaded_states,
                "plan": plan.to_dict(),
            }
            _atomic_write(
                stage / "manifest.json",
                _json_bytes(manifest),
                mode=0o600,
            )
            os.replace(stage, final)
            _fsync_directory(final.parent)
            return final
        except Exception:
            _remove_path(stage)
            raise

    def activate(self, plan: InstallationPlan, *, confirm: bool = False) -> InstallationResult:
        """Apply a validated plan, automatically rolling back on any failure."""

        if not confirm:
            raise PermissionError("activation requires confirm=True")
        if plan.issues:
            raise ValueError("cannot activate an unsafe plan: " + "; ".join(plan.issues))

        backup_dir = self.create_backup(plan)
        manifest = self._load_manifest(backup_dir)
        commands: list[CommandExecution] = []
        try:
            # Existing Vox jobs must be booted out before replacing their app/plists.
            for label in VOX_LABELS:
                if manifest["loaded_states"][label]["loaded"]:
                    commands.append(self._launchctl("bootout", f"{self._domain}/{label}"))
                    _require_command(commands[-1], f"stop existing {label}")

            for directory in plan.directories:
                _ensure_private_directory(directory, self.paths.home)

            for item in plan.files:
                self._apply_planned_file(item)

            for command in plan.host_commands:
                result = self._run(command.argv)
                commands.append(result)
                if not result.ok and not command.optional:
                    _require_command(result, command.description)

            self._set_codex_timeout(600)

            # Only now, after backup + new files + host registration, touch legacy jobs.
            for label in plan.unload_labels:
                if manifest["loaded_states"][label]["loaded"]:
                    result = self._launchctl("bootout", f"{self._domain}/{label}")
                    commands.append(result)
                    _require_command(result, f"unload legacy service {label}")

            # Start backends first, then the app/runtime that consumes them.
            for label in ("com.vox.whisper", "com.vox.kokoro", "com.vox.runtime"):
                result = self._launchctl(
                    "bootstrap",
                    self._domain,
                    str(self.paths.plist_for_label(label)),
                )
                commands.append(result)
                _require_command(result, f"bootstrap {label}")

            # A loaded job is not proof that its endpoint is usable. Seal the
            # migration only after every replacement answers on loopback.
            health_targets = (
                ("Whisper", "http://127.0.0.1:2022/health", 90.0),
                ("Kokoro", "http://127.0.0.1:8880/health", 120.0),
                ("Vox runtime", "http://127.0.0.1:8766/health", 120.0),
            )
            for name, url, timeout in health_targets:
                if not self._health_checker(url, timeout):
                    raise RuntimeError(
                        f"{name} failed its loopback health canary: {url}"
                    )

            manifest["activated"] = True
            manifest["activated_at"] = _iso(self._now())
            manifest["activation_commands"] = [command.to_dict() for command in commands]
            _atomic_write(
                backup_dir / "manifest.json",
                _json_bytes(manifest),
                mode=0o600,
            )
            return InstallationResult(True, backup_dir, False, tuple(commands))
        except Exception as exc:
            rollback = self.rollback(backup_dir, confirm=True)
            commands.extend(rollback.commands)
            return InstallationResult(
                False,
                backup_dir,
                rollback.success,
                tuple(commands),
                error=f"{type(exc).__name__}: {exc}",
            )

    def rollback(self, backup_dir: Path, *, confirm: bool = False) -> RollbackResult:
        """Restore one explicit snapshot and its exact pre-migration loaded states."""

        if not confirm:
            raise PermissionError("rollback requires confirm=True")
        backup = Path(backup_dir).expanduser().resolve()
        rollback_root = self.paths.rollback_root.resolve()
        if backup.parent != rollback_root:
            raise ValueError(f"backup is outside the Vox rollback root: {backup}")
        manifest = self._load_manifest(backup)
        commands: list[CommandExecution] = []
        restored: list[Path] = []
        try:
            # Validate every destination and backup hash before stopping a
            # service or restoring the first byte. A damaged snapshot must
            # fail closed, not leave a half-restored machine.
            self._validate_restore_entries(backup, manifest["entries"])

            # Stop current Vox jobs before restoring app/plist/config bytes.
            for label in VOX_LABELS:
                if self._capture_loaded_state(label)["loaded"]:
                    result = self._launchctl("bootout", f"{self._domain}/{label}")
                    commands.append(result)
                    _require_command(result, f"stop {label} for rollback")

            for entry in reversed(manifest["entries"]):
                target = Path(entry["path"])
                _require_under_home(target, self.paths.home, "rollback target")
                self._restore_entry(backup, entry)
                restored.append(target)

            # Reconcile every tracked job to the state captured before activation.
            for label in TRACKED_LABELS:
                wanted = bool(manifest["loaded_states"][label]["loaded"])
                current = bool(self._capture_loaded_state(label)["loaded"])
                if current and not wanted:
                    result = self._launchctl("bootout", f"{self._domain}/{label}")
                    commands.append(result)
                    _require_command(result, f"unload {label} during rollback")
                elif wanted and not current:
                    plist_path = self.paths.plist_for_label(label)
                    if not plist_path.is_file():
                        raise FileNotFoundError(
                            f"cannot restore loaded state for {label}; plist missing: {plist_path}"
                        )
                    result = self._launchctl("bootstrap", self._domain, str(plist_path))
                    commands.append(result)
                    _require_command(result, f"restore loaded state for {label}")

            manifest["rolled_back_at"] = _iso(self._now())
            _atomic_write(
                backup / "manifest.json",
                _json_bytes(manifest),
                mode=0o600,
            )
            return RollbackResult(True, backup, tuple(restored), tuple(commands))
        except Exception as exc:
            return RollbackResult(
                False,
                backup,
                tuple(restored),
                tuple(commands),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _validation_issues(self) -> list[str]:
        required_files = {
            "Vox.app executable": self.paths.app_source / "Contents" / "MacOS" / "VoxStatus",
            "VOX_RUNTIME": self.paths.runtime_executable,
            "whisper-server": self.paths.whisper_binary,
            "Whisper model": self.paths.whisper_model,
            "Kokoro Python": self.paths.kokoro_python,
            "Claude CLI": self.paths.claude_cli,
            "Codex CLI": self.paths.codex_cli,
            "launchctl": self.paths.launchctl,
            "Vox skill": self.paths.skill_source / "SKILL.md",
        }
        issues = [f"{name} is missing: {path}" for name, path in required_files.items() if not path.is_file()]
        for name in ("VOX_RUNTIME", "whisper-server", "Kokoro Python", "Claude CLI", "Codex CLI"):
            path = required_files[name]
            if path.is_file() and not os.access(path, os.X_OK):
                issues.append(f"{name} is not executable: {path}")
        if not self.paths.app_source.is_dir():
            issues.append(f"Vox.app source is missing: {self.paths.app_source}")
        if not self.paths.skill_source.is_dir():
            issues.append(f"Vox skill directory is missing: {self.paths.skill_source}")
        return issues

    def _run(self, argv: Sequence[str]) -> CommandExecution:
        command = tuple(str(part) for part in argv)
        if not command or not Path(command[0]).is_absolute():
            return CommandExecution(command, None, error="external command must use an absolute executable")
        try:
            result = self._runner(
                list(command),
                capture_output=True,
                text=True,
                timeout=self._command_timeout,
                check=False,
                shell=False,
            )
            return CommandExecution(
                command,
                result.returncode,
                result.stdout or "",
                result.stderr or "",
            )
        except subprocess.TimeoutExpired as exc:
            return CommandExecution(
                command,
                None,
                stdout=_coerce_output(exc.stdout),
                stderr=_coerce_output(exc.stderr),
                error=f"command timed out after {self._command_timeout:.1f}s",
            )
        except OSError as exc:
            return CommandExecution(command, None, error=f"{type(exc).__name__}: {exc}")

    def _launchctl(self, *args: str) -> CommandExecution:
        return self._run((str(self.paths.launchctl), *args))

    def _capture_loaded_state(self, label: str) -> dict[str, Any]:
        result = self._launchctl("print", f"{self._domain}/{label}")
        state = _launchctl_field(result.stdout, "state") if result.ok else None
        return {
            "loaded": result.ok,
            "state": state,
            "returncode": result.returncode,
            "state_sha256": _sha256(result.stdout.encode()) if result.ok else None,
            "error": None if result.ok else result.error or _first_nonempty(result.stderr, result.stdout),
            "plist_path": str(self.paths.plist_for_label(label)),
        }

    def _backup_one(self, path: Path, stage: Path, index: int) -> dict[str, Any]:
        _require_under_home(path, self.paths.home, "backup target")
        entry: dict[str, Any] = {
            "path": str(path),
            "kind": "missing",
            "backup": None,
            "sha256": None,
            "mode": None,
        }
        if not os.path.lexists(path):
            return entry

        metadata = path.lstat()
        entry["mode"] = stat.S_IMODE(metadata.st_mode)
        backup_name = f"{index:03d}-{hashlib.sha256(str(path).encode()).hexdigest()[:12]}-{path.name}"
        relative = Path("files") / backup_name
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)

        if path.is_symlink():
            target = os.readlink(path)
            destination.symlink_to(target)
            entry.update(
                kind="symlink",
                backup=str(relative),
                sha256=_sha256(("symlink\0" + target).encode()),
            )
        elif path.is_file():
            shutil.copy2(path, destination)
            entry.update(kind="file", backup=str(relative), sha256=_hash_file(path))
        elif path.is_dir():
            shutil.copytree(path, destination, symlinks=True, copy_function=shutil.copy2)
            entry.update(kind="directory", backup=str(relative), sha256=_hash_tree(path))
        else:
            raise ValueError(f"unsupported backup target type: {path}")
        return entry

    def _apply_planned_file(self, item: PlannedFile) -> None:
        _require_under_home(item.target, self.paths.home, "installation target")
        if item.kind == "bytes":
            if item.content is None:
                raise ValueError(f"byte plan has no content: {item.description}")
            if item.sha256 and _sha256(item.content) != item.sha256:
                raise ValueError(f"planned content hash changed: {item.description}")
            _atomic_write(item.target, item.content, mode=item.mode)
            return
        if item.source is None or not item.source.is_dir():
            raise FileNotFoundError(f"planned source tree is missing: {item.source}")
        if item.sha256 and _hash_tree(item.source) != item.sha256:
            raise ValueError(f"planned source tree changed: {item.source}")
        _atomic_replace_tree(item.source, item.target)

    def _set_codex_timeout(self, seconds: int) -> None:
        current = self.paths.codex_config.read_text(encoding="utf-8")
        updated = set_codex_vox_tool_timeout(current, seconds)
        mode = stat.S_IMODE(self.paths.codex_config.stat().st_mode)
        _atomic_write(self.paths.codex_config, updated.encode(), mode=mode)

    def _load_manifest(self, backup_dir: Path) -> dict[str, Any]:
        manifest_path = Path(backup_dir) / "manifest.json"
        with manifest_path.open("rb") as handle:
            manifest = json.load(handle)
        if manifest.get("manifest_version") != MANIFEST_VERSION:
            raise ValueError("unsupported rollback manifest version")
        if Path(manifest.get("home", "")) != self.paths.home:
            raise ValueError("rollback manifest belongs to a different home directory")
        labels = manifest.get("loaded_states", {})
        if any(label not in labels for label in TRACKED_LABELS):
            raise ValueError("rollback manifest is missing launchd loaded-state data")
        return manifest

    def _restore_entry(self, backup_dir: Path, entry: Mapping[str, Any]) -> None:
        target = Path(str(entry["path"]))
        kind = entry["kind"]
        if kind == "missing":
            _remove_path(target)
            return
        relative = Path(str(entry["backup"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe backup entry: {relative}")
        source = backup_dir / relative
        expected = entry["sha256"]
        if kind == "file":
            if _hash_file(source) != expected:
                raise ValueError(f"backup hash mismatch for {target}")
            _atomic_write(target, source.read_bytes(), mode=int(entry["mode"]))
        elif kind == "directory":
            if _hash_tree(source) != expected:
                raise ValueError(f"backup tree hash mismatch for {target}")
            _atomic_replace_tree(source, target)
        elif kind == "symlink":
            link_target = os.readlink(source)
            if _sha256(("symlink\0" + link_target).encode()) != expected:
                raise ValueError(f"backup symlink hash mismatch for {target}")
            _atomic_symlink(link_target, target)
        else:
            raise ValueError(f"unsupported manifest entry kind: {kind!r}")

    def _validate_restore_entries(
        self,
        backup_dir: Path,
        entries: Sequence[Mapping[str, Any]],
    ) -> None:
        for entry in entries:
            target = Path(str(entry.get("path", "")))
            _require_under_home(target, self.paths.home, "rollback target")
            kind = entry.get("kind")
            if kind == "missing":
                continue
            relative = Path(str(entry.get("backup", "")))
            if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe backup entry: {relative}")
            source = backup_dir / relative
            expected = entry.get("sha256")
            if kind == "file":
                actual = _hash_file(source)
            elif kind == "directory":
                actual = _hash_tree(source)
            elif kind == "symlink":
                link_target = os.readlink(source)
                actual = _sha256(("symlink\0" + link_target).encode())
            else:
                raise ValueError(f"unsupported manifest entry kind: {kind!r}")
            if actual != expected:
                raise ValueError(f"backup hash mismatch for {target}")


def set_codex_vox_tool_timeout(text: str, seconds: int = 600) -> str:
    """Set only ``mcp_servers.vox.tool_timeout_sec`` while preserving TOML text."""

    if seconds <= 0:
        raise ValueError("tool timeout must be positive")
    parsed = tomllib.loads(text) if text.strip() else {}
    section_re = re.compile(
        r"^\s*\[\s*mcp_servers\.(?:vox|\"vox\"|'vox')\s*\]\s*(?:#.*)?$"
    )
    any_section_re = re.compile(r"^\s*\[[^]]+\]\s*(?:#.*)?$")
    assignment_re = re.compile(r"^\s*tool_timeout_sec\s*=")
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if section_re.match(line.rstrip("\r\n"))), None)

    existing_vox = parsed.get("mcp_servers", {}).get("vox") if isinstance(parsed.get("mcp_servers", {}), dict) else None
    if start is None and existing_vox is not None:
        raise ValueError("Codex vox configuration is not in a dedicated [mcp_servers.vox] section")

    newline = "\r\n" if "\r\n" in text else "\n"
    assignment = f"tool_timeout_sec = {seconds}{newline}"
    if start is None:
        prefix = text
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        if prefix and not prefix.endswith(newline * 2):
            prefix += newline
        updated = prefix + f"[mcp_servers.vox]{newline}" + assignment
    else:
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if any_section_re.match(lines[index].rstrip("\r\n"))
            ),
            len(lines),
        )
        output = lines[: start + 1]
        replaced = False
        for line in lines[start + 1 : end]:
            if assignment_re.match(line):
                if not replaced:
                    output.append(assignment)
                    replaced = True
                continue
            output.append(line)
        if not replaced:
            output.append(assignment)
        output.extend(lines[end:])
        updated = "".join(output)

    verified = tomllib.loads(updated)
    value = verified.get("mcp_servers", {}).get("vox", {}).get("tool_timeout_sec")
    if value != seconds:
        raise ValueError("failed to set Codex Vox tool timeout safely")
    return updated


def _validate_launchd_document(label: str, document: Mapping[str, Any]) -> None:
    if document.get("Label") != label:
        raise ValueError(f"launchd label mismatch for {label}")
    arguments = document.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments or not Path(arguments[0]).is_absolute():
        raise ValueError(f"{label} must use an absolute direct executable")
    lowered = [str(argument).lower() for argument in arguments]
    forbidden = {"uv", "uvx", "pip", "install", "download", "curl", "wget"}
    if any(Path(argument).name in forbidden or argument in forbidden for argument in lowered):
        raise ValueError(f"{label} contains mutable startup work")
    if any("limit-max-requests" in argument or "0.0.0.0" in argument for argument in lowered):
        raise ValueError(f"{label} contains an unsafe runtime argument")
    if label in {"com.vox.whisper", "com.vox.kokoro"}:
        try:
            host = arguments[arguments.index("--host") + 1]
        except (ValueError, IndexError) as exc:
            raise ValueError(f"{label} is missing an explicit host") from exc
        if host != "127.0.0.1":
            raise ValueError(f"{label} must bind to 127.0.0.1")
    if label == "com.vox.runtime":
        runtime = document.get("EnvironmentVariables", {}).get("VOX_RUNTIME")
        if not runtime or not Path(runtime).is_absolute():
            raise ValueError("com.vox.runtime requires an absolute VOX_RUNTIME")


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, mode)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _ensure_private_directory(path: Path, home: Path) -> None:
    _require_under_home(path, home, "installation directory")
    if os.path.lexists(path) and not path.is_dir():
        raise NotADirectoryError(f"installation directory is not a directory: {path}")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)


def _atomic_replace_tree(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
    previous = target.parent / f".{target.name}.old-{uuid.uuid4().hex}"
    shutil.copytree(source, stage, symlinks=True, copy_function=shutil.copy2)
    had_target = os.path.lexists(target)
    try:
        if had_target:
            os.replace(target, previous)
        os.replace(stage, target)
        _fsync_directory(target.parent)
    except Exception:
        _remove_path(stage)
        if had_target and os.path.lexists(previous) and not os.path.lexists(target):
            os.replace(previous, target)
        raise
    else:
        _remove_path(previous)


def _atomic_symlink(link_target: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
    previous = target.parent / f".{target.name}.old-{uuid.uuid4().hex}"
    temporary.symlink_to(link_target)
    had_target = os.path.lexists(target)
    try:
        if had_target:
            os.replace(target, previous)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except Exception:
        _remove_path(temporary)
        if had_target and os.path.lexists(previous) and not os.path.lexists(target):
            os.replace(previous, target)
        raise
    else:
        _remove_path(previous)


def _remove_path(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()):
        relative = item.relative_to(path).as_posix()
        if item.is_symlink():
            marker = "L"
            content_hash = _sha256(("symlink\0" + os.readlink(item)).encode())
        elif item.is_dir():
            marker = "D"
            content_hash = ""
        elif item.is_file():
            marker = "F"
            content_hash = _hash_file(item)
        else:
            raise ValueError(f"unsupported tree entry: {item}")
        digest.update(f"{marker}\0{relative}\0{content_hash}\n".encode())
    return digest.hexdigest()


def _hash_path_if_present(path: Path) -> str | None:
    if path.is_dir():
        return _hash_tree(path)
    if path.is_file():
        return _hash_file(path)
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y%m%dT%H%M%S.%fZ")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _which_absolute(command: str) -> Path:
    resolved = shutil.which(command)
    if resolved:
        return Path(resolved).resolve()
    # Keep discovery side-effect free; validation will report this placeholder.
    return Path("/usr/bin") / command


def _require_under_home(path: Path, home: Path, name: str) -> None:
    candidate = Path(path).expanduser().resolve(strict=False)
    root = Path(home).expanduser().resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"{name} must be inside {root}: {candidate}")


def _dedupe_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return tuple(result)


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _first_nonempty(*values: str) -> str | None:
    return next((value.strip() for value in values if value and value.strip()), None)


def _wait_for_http_health(url: str, timeout: float) -> bool:
    """Poll a loopback health URL without consulting proxy environment state."""

    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, method="GET")
            remaining = max(0.1, deadline - time.monotonic())
            with opener.open(request, timeout=min(2.0, remaining)) as response:
                if 200 <= int(response.status) < 300:
                    return True
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(0.25)
    return False


def _launchctl_field(text: str, field: str) -> str | None:
    matched = re.search(rf"^\s*{re.escape(field)} = (.+?)\s*$", text, re.MULTILINE)
    return matched.group(1).strip() if matched else None


def _require_command(result: CommandExecution, description: str) -> None:
    if result.ok:
        return
    detail = result.error or _first_nonempty(result.stderr, result.stdout) or "unknown error"
    raise RuntimeError(f"{description} failed: {detail}")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "CommandExecution",
    "InstallationPlan",
    "InstallationResult",
    "InstallerPaths",
    "LEGACY_BACKEND_LABELS",
    "MANIFEST_VERSION",
    "MCP_URL",
    "PlannedCommand",
    "PlannedFile",
    "RollbackResult",
    "STALE_LABELS",
    "TRACKED_LABELS",
    "TransactionalInstaller",
    "VOX_LABELS",
    "generate_launchd_plists",
    "set_codex_vox_tool_timeout",
]
