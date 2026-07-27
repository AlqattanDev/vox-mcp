"""Thin Click client for the persistent, loopback-only Vox runtime."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import subprocess
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import click
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from . import __version__
from .config import load_user_settings
from .errors import ConfigurationError
from .installer import TransactionalInstaller


MCP_URL = "http://127.0.0.1:8766/mcp"
HEALTH_URL = "http://127.0.0.1:8766/health"
DEFAULT_READ_TIMEOUT_SECONDS = 600.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 15.0
RUNTIME_LABEL = "com.vox.runtime"


class MCPCallError(RuntimeError):
    """A structured MCP tool call failed."""


class RuntimeStartError(RuntimeError):
    """The already-installed Vox runtime could not be started."""


class ToolCaller(Protocol):
    def __call__(self, tool_name: str, arguments: Mapping[str, Any]) -> Any: ...


class MCPToolCaller:
    """Open one bounded Streamable HTTP session and invoke one Vox tool."""

    def __init__(
        self,
        *,
        url: str = MCP_URL,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        if url != MCP_URL:
            raise ValueError(f"the Vox CLI only connects to {MCP_URL}")
        if read_timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise ValueError("MCP timeouts must be positive")
        self.url = url
        self.read_timeout_seconds = float(read_timeout_seconds)
        self.connect_timeout_seconds = float(connect_timeout_seconds)

    def __call__(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        return asyncio.run(self.call(tool_name, arguments))

    async def call(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        read_timeout = timedelta(seconds=self.read_timeout_seconds)
        async with streamablehttp_client(
            self.url,
            timeout=self.connect_timeout_seconds,
            sse_read_timeout=self.read_timeout_seconds,
            terminate_on_close=True,
        ) as (read_stream, write_stream, _session_id):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=read_timeout,
                client_info=Implementation(name="vox-cli", version=__version__),
            ) as session:
                await session.initialize()
                result = await session.call_tool(
                    tool_name,
                    dict(arguments),
                    read_timeout_seconds=read_timeout,
                )
        return _extract_tool_result(result)


class InstalledRuntimeStarter:
    """Start only the launchd job produced by ``TransactionalInstaller``."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        uid: int | None = None,
    ) -> None:
        self._runner = runner
        self._client_factory = client_factory
        self._clock = clock
        self._sleeper = sleeper
        self._uid = os.getuid() if uid is None else uid

    def __call__(self, installer: Any, wait_seconds: float = 15.0) -> dict[str, Any]:
        paths = installer.paths
        runtime = Path(paths.runtime_executable)
        plist = Path(paths.runtime_plist)
        launchctl = Path(paths.launchctl)

        if not runtime.is_file() or not os.access(runtime, os.X_OK):
            raise RuntimeStartError(
                f"built runtime is not installed or executable: {runtime}; run `vox install` first"
            )
        if not plist.is_file():
            raise RuntimeStartError(
                f"runtime LaunchAgent is not installed: {plist}; run `vox install` first"
            )
        if not launchctl.is_file() or not os.access(launchctl, os.X_OK):
            raise RuntimeStartError(f"launchctl is unavailable: {launchctl}")

        healthy = self._health()
        if healthy is not None:
            return {
                "started": False,
                "already_healthy": True,
                "runtime": str(runtime),
                "health": healthy,
            }

        domain = f"gui/{self._uid}"
        service = f"{domain}/{RUNTIME_LABEL}"
        inspected = self._run((str(launchctl), "print", service))
        if inspected.returncode == 0:
            command = (str(launchctl), "kickstart", "-k", service)
            action = "kickstart"
        else:
            command = (str(launchctl), "bootstrap", domain, str(plist))
            action = "bootstrap"
        launched = self._run(command)
        if launched.returncode != 0:
            detail = _command_output(launched) or f"exit code {launched.returncode}"
            raise RuntimeStartError(f"launchctl {action} failed: {detail}")

        deadline = self._clock() + max(0.0, wait_seconds)
        health = self._health()
        while health is None and self._clock() < deadline:
            self._sleeper(min(0.25, max(0.0, deadline - self._clock())))
            health = self._health()
        if health is None:
            raise RuntimeStartError(
                f"runtime was launched but {HEALTH_URL} did not become healthy"
            )
        return {
            "started": True,
            "already_healthy": False,
            "action": action,
            "command": list(command),
            "runtime": str(runtime),
            "health": health,
        }

    def _run(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return self._runner(
            list(argv),
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )

    def _health(self) -> dict[str, Any] | None:
        try:
            with self._client_factory(
                timeout=1.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.get(HEALTH_URL)
            if response.status_code != 200:
                return None
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("healthy") is False:
                return None
            return payload
        except (httpx.HTTPError, OSError, ValueError):
            return None


@dataclass(slots=True)
class _Dependencies:
    caller: ToolCaller
    installer_factory: Callable[[], Any]
    runtime_starter: Callable[[Any, float], Any]
    installer: Any | None = None

    def get_installer(self) -> Any:
        if self.installer is None:
            self.installer = self.installer_factory()
        return self.installer


def create_cli(
    *,
    mcp_caller: ToolCaller | None = None,
    installer: Any | None = None,
    installer_factory: Callable[[], Any] | None = None,
    runtime_starter: Callable[[Any, float], Any] | None = None,
) -> click.Group:
    """Create the CLI with injectable side-effect boundaries for tests."""

    if installer is not None and installer_factory is not None:
        raise ValueError("pass installer or installer_factory, not both")
    if installer is not None:
        resolved_factory = lambda: installer
    else:
        resolved_factory = installer_factory or TransactionalInstaller
    dependencies = _Dependencies(
        caller=mcp_caller or MCPToolCaller(),
        installer_factory=resolved_factory,
        runtime_starter=runtime_starter or InstalledRuntimeStarter(),
    )

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.version_option(version=__version__, prog_name="vox")
    def cli() -> None:
        """Control the persistent local Vox voice runtime."""

        # The CLI must read the same configuration the runtime does, or a knob
        # set with `vox set` is honoured by the daemon and silently ignored by
        # the tool that measures it — calibration would report against defaults
        # the runtime is not using.
        try:
            load_user_settings()
        except ConfigurationError as exc:
            raise click.ClickException(str(exc)) from exc

    @cli.command("status")
    def status_command() -> None:
        """Show session, ownership, and microphone state."""

        _invoke_and_print(dependencies, "voice_session", {"action": "status"})

    @cli.group("session")
    def session_group() -> None:
        """Control the long-lived voice session."""

    @session_group.command("start")
    def session_start() -> None:
        _invoke_and_print(dependencies, "voice_session", {"action": "start"})

    @session_group.command("stop")
    def session_stop() -> None:
        _invoke_and_print(dependencies, "voice_session", {"action": "stop"})

    @session_group.command("pause")
    @click.option("--seconds", type=click.FloatRange(min=0.0), default=None)
    def session_pause(seconds: float | None) -> None:
        _invoke_and_print(
            dependencies,
            "voice_session",
            _drop_none({"action": "pause", "seconds": seconds}),
        )

    @session_group.command("resume")
    def session_resume() -> None:
        _invoke_and_print(dependencies, "voice_session", {"action": "resume"})

    @cli.command("speak")
    @click.argument("message")
    @click.option("--voice")
    @click.option("--speed", type=click.FloatRange(min=0.25, max=4.0))
    @click.option("--interruptible/--no-interruptible", default=True, show_default=True)
    def speak_command(
        message: str,
        voice: str | None,
        speed: float | None,
        interruptible: bool,
    ) -> None:
        """Speak without opening the microphone."""

        _invoke_and_print(
            dependencies,
            "speak",
            _drop_none(
                {
                    "message": message,
                    "voice": voice,
                    "speed": speed,
                    "interruptible": interruptible,
                }
            ),
        )

    def listen_options(function: Callable[..., Any]) -> Callable[..., Any]:
        function = click.option("--language")(function)
        function = click.option(
            "--trailing-silence-s",
            type=click.FloatRange(min=0.1, max=10.0),
            default=1.6,
            show_default=True,
        )(function)
        function = click.option(
            "--listen-duration-min",
            type=click.FloatRange(min=0.0, max=300.0),
            default=0.5,
            show_default=True,
        )(function)
        return click.option(
            "--listen-duration-max",
            type=click.FloatRange(min=0.1, max=300.0),
            default=75.0,
            show_default=True,
        )(function)

    @cli.command("listen")
    @listen_options
    def listen_command(
        listen_duration_max: float,
        listen_duration_min: float,
        trailing_silence_s: float,
        language: str | None,
    ) -> None:
        """Listen for one bounded utterance, then close the microphone."""

        _invoke_and_print(
            dependencies,
            "listen",
            _drop_none(
                {
                    "listen_duration_max": listen_duration_max,
                    "listen_duration_min": listen_duration_min,
                    "trailing_silence_s": trailing_silence_s,
                    "language": language,
                }
            ),
        )

    @cli.command("converse")
    @click.argument("message")
    @click.option("--wait/--no-wait", "wait_for_response", default=True, show_default=True)
    @click.option("--voice")
    @click.option("--speed", type=click.FloatRange(min=0.25, max=4.0))
    @click.option(
        "--trailing-silence-s",
        type=click.FloatRange(min=0.1, max=10.0),
        default=1.6,
        show_default=True,
    )
    @click.option(
        "--listen-duration-min",
        type=click.FloatRange(min=0.0, max=300.0),
        default=0.5,
        show_default=True,
    )
    @click.option(
        "--listen-duration-max",
        type=click.FloatRange(min=0.1, max=300.0),
        default=75.0,
        show_default=True,
    )
    def converse_command(
        message: str,
        wait_for_response: bool,
        voice: str | None,
        speed: float | None,
        trailing_silence_s: float,
        listen_duration_min: float,
        listen_duration_max: float,
    ) -> None:
        """Speak and optionally listen for one response."""

        _invoke_and_print(
            dependencies,
            "converse",
            _drop_none(
                {
                    "message": message,
                    "wait_for_response": wait_for_response,
                    "voice": voice,
                    "speed": speed,
                    "listen_duration_max": listen_duration_max,
                    "listen_duration_min": listen_duration_min,
                    "trailing_silence_s": trailing_silence_s,
                }
            ),
        )

    @cli.group("control")
    def control_group() -> None:
        """Interrupt the active turn without relying on the agent host."""

    @control_group.command("cancel")
    def control_cancel() -> None:
        _invoke_and_print(dependencies, "voice_control", {"action": "cancel"})

    @control_group.command("manual-end")
    def control_manual_end() -> None:
        _invoke_and_print(dependencies, "voice_control", {"action": "end_turn"})

    @control_group.command("repeat")
    def control_repeat() -> None:
        _invoke_and_print(dependencies, "voice_control", {"action": "repeat"})

    @control_group.command("deliver-text")
    @click.argument("text", nargs=-1, required=True)
    def control_deliver_text(text: tuple[str, ...]) -> None:
        """End an in-flight listen and send TEXT as the turn instead.

        For when you would rather type than talk while the mic is already open:
        the listen returns your text immediately instead of making you wait it
        out. A no-op when nothing is listening.
        """

        _invoke_and_print(
            dependencies,
            "voice_control",
            {"action": "deliver_text", "text": " ".join(text)},
        )

    @cli.command("set")
    @click.argument("assignments", nargs=-1)
    @click.option("--unset", multiple=True, help="Remove a setting.")
    @click.option("--restart/--no-restart", default=True, show_default=True)
    def set_command(assignments: tuple[str, ...], unset: tuple[str, ...], restart: bool) -> None:
        """Persist runtime settings, e.g. `vox set VOX_COMPANION_ENABLED=1`.

        Settings live in ~/.vox/settings.json because the runtime is started by
        launchd, which never sees your shell environment. With no arguments this
        prints the current file.
        """

        from .config import user_settings_path

        path = user_settings_path()
        try:
            current = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            current = {}

        if not assignments and not unset:
            click.echo(json.dumps(current, indent=2, sort_keys=True))
            return

        for item in assignments:
            key, _, value = item.partition("=")
            key = key.strip()
            if not _ or not key.startswith("VOX_"):
                raise click.ClickException(
                    f"expected VOX_NAME=value, got {item!r}"
                )
            current[key] = value.strip()
        for key in unset:
            current.pop(key.strip(), None)

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)
        click.echo(json.dumps(current, indent=2, sort_keys=True))

        if restart:
            subprocess.run(
                ["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{RUNTIME_LABEL}"],
                capture_output=True,
                check=False,
            )
            click.echo(f"restarted {RUNTIME_LABEL}", err=True)

    @cli.group("barge-in")
    def barge_in_group() -> None:
        """Measure and tune the echo gate that lets you interrupt playback."""

    @barge_in_group.command("calibrate")
    @click.option(
        "--seconds",
        type=click.FloatRange(min=2.0, max=30.0),
        default=8.0,
        show_default=True,
        help="Length of each measurement window.",
    )
    @click.option("--safety-db", type=click.FloatRange(min=0.0, max=20.0), default=6.0,
                  show_default=True, help="Headroom subtracted from the measured gap.")
    @click.option(
        "--countdown",
        type=click.FloatRange(min=0.0, max=30.0),
        default=None,
        help=(
            "Skip the keypress and start the voice pass after this many seconds. "
            "Use 0 for no pause. Required when an agent drives calibration, since "
            "nothing is there to press a key."
        ),
    )
    def barge_in_calibrate(seconds: float, safety_db: float, countdown: float | None) -> None:
        """Measure speaker bleed against your voice and print a real margin.

        Barge-in has no acoustic echo cancellation to lean on, so the gate is
        only as good as the numbers behind it. This measures both sides on
        your actual hardware instead of guessing.
        """

        measurement = _calibrate_barge_in(
            dependencies,
            seconds=seconds,
            safety_db=safety_db,
            countdown=countdown,
        )
        click.echo(json.dumps(measurement, indent=2, sort_keys=True))

    @cli.command("doctor")
    @click.option("--include-logs", is_flag=True)
    def doctor_command(include_logs: bool) -> None:
        """Inspect local audio, services, dependencies, privacy, and events."""

        _invoke_and_print(
            dependencies,
            "diagnostics",
            {"section": "all", "include_logs": include_logs},
        )

    @cli.command("service")
    @click.argument(
        "action",
        type=click.Choice(["status", "health", "start", "stop", "restart", "logs"]),
        default="status",
        required=False,
    )
    @click.argument(
        "service_name",
        type=click.Choice(["all", "whisper", "kokoro", "vox"]),
        default="all",
        required=False,
    )
    @click.option("--lines", type=click.IntRange(min=1, max=2_000), default=80)
    def service_command(action: str, service_name: str, lines: int) -> None:
        """Inspect or manage a local supervised service."""

        _invoke_and_print(
            dependencies,
            "service",
            {"action": action, "service_name": service_name, "lines": lines},
        )

    @cli.command("transcribe")
    @click.argument(
        "audio_file",
        required=False,
        type=click.Path(
            path_type=Path,
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    )
    @click.option("--latest", is_flag=True, help="Transcribe the latest captured utterance.")
    @click.option("--language")
    @click.option(
        "--output-format",
        type=click.Choice(["text", "json", "srt", "vtt", "csv"]),
        default="text",
        show_default=True,
    )
    @click.option("--word-timestamps", is_flag=True)
    def transcribe_command(
        audio_file: Path | None,
        latest: bool,
        language: str | None,
        output_format: str,
        word_timestamps: bool,
    ) -> None:
        """Transcribe a local audio file with local Whisper."""

        if audio_file is None and not latest:
            raise click.UsageError("provide AUDIO_FILE or --latest")
        if audio_file is not None and latest:
            raise click.UsageError("AUDIO_FILE and --latest are mutually exclusive")

        _invoke_and_print(
            dependencies,
            "transcribe",
            _drop_none(
                {
                    "audio_file": str(audio_file) if audio_file else None,
                    "latest": True if latest else None,
                    "language": language,
                    "output_format": output_format,
                    "word_timestamps": word_timestamps,
                }
            ),
        )

    @cli.command("voices")
    @click.option("--provider")
    def voices_command(provider: str | None) -> None:
        """List available local TTS voices."""

        _invoke_and_print(
            dependencies,
            "voice_registry",
            _drop_none({"provider": provider}),
        )

    @cli.command("install")
    @click.option(
        "--activate",
        is_flag=True,
        help="Apply the displayed transaction instead of performing a dry run.",
    )
    @click.option(
        "--yes",
        is_flag=True,
        help="Required with --activate; no interactive confirmation is inferred.",
    )
    def install_command(activate: bool, yes: bool) -> None:
        """Plan installation by default; activation requires two explicit flags."""

        if activate != yes:
            raise click.UsageError("activation requires both --activate and --yes")
        selected_installer = dependencies.get_installer()
        try:
            result = selected_installer.install(
                dry_run=not activate,
                confirm_activation=activate and yes,
            )
        except Exception as exc:
            raise click.ClickException(f"installation failed: {exc}") from exc
        _print_json(result)

    @cli.command("rollback")
    @click.argument(
        "backup_dir",
        type=click.Path(
            path_type=Path,
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    )
    @click.option("--yes", is_flag=True, help="Explicitly confirm restoration.")
    def rollback_command(backup_dir: Path, yes: bool) -> None:
        """Restore one explicit transactional backup."""

        if not yes:
            raise click.UsageError("rollback requires --yes")
        try:
            result = dependencies.get_installer().rollback(backup_dir, confirm=True)
        except Exception as exc:
            raise click.ClickException(f"rollback failed: {exc}") from exc
        _print_json(result)

    @cli.command("start-runtime")
    @click.option(
        "--wait-seconds",
        type=click.FloatRange(min=0.0, max=60.0),
        default=15.0,
        show_default=True,
    )
    def start_runtime_command(wait_seconds: float) -> None:
        """Start and health-check only the already-built installed runtime."""

        try:
            result = dependencies.runtime_starter(
                dependencies.get_installer(), wait_seconds
            )
        except Exception as exc:
            raise click.ClickException(f"could not start runtime: {exc}") from exc
        _print_json(result)

    return cli


CALIBRATION_SCRIPT = (
    "Calibrating the barge-in gate now. Keep quiet while I talk, because this "
    "first pass measures how much of my own voice your microphone can hear "
    "coming back through the speakers. I will keep going for a few seconds so "
    "there is enough signal to be worth measuring, and then it will be your "
    "turn to say something."
)


def _measure_input(seconds: float) -> Any:
    """Watch the microphone for one window using the production recorder."""

    from .audio import AudioRecorder, CaptureConfig

    recorder = AudioRecorder(CaptureConfig(save_latest=False, latest_wav_path=None))
    return recorder.measure(seconds, device=os.environ.get("VOX_INPUT_DEVICE"))


def _announce(dependencies: "_Dependencies", message: str) -> None:
    """Say one line through the runtime, best-effort, blocking until spoken.

    Calibration may be driven from a terminal the user is not looking at, so
    the instructions have to be audible. It blocks so the announcement cannot
    still be playing once a measurement window opens and land in the numbers.
    A runtime that will not speak must not abort the measurement.
    """

    try:
        result = dependencies.caller("speak", {"message": message})
        if inspect.isawaitable(result):
            asyncio.run(result)
    except Exception:
        pass


def _calibrate_barge_in(
    dependencies: "_Dependencies",
    *,
    seconds: float,
    safety_db: float,
    countdown: float | None = None,
) -> dict[str, Any]:
    import threading

    duck = min(1.0, max(0.0, float(os.environ.get("VOX_BARGE_IN_DUCK_VOLUME", "0.85"))))
    duck_offset_db = 20.0 * math.log10(duck) if duck > 0 else -96.0

    click.echo("Vox is opening the microphone for calibration. Stay quiet for the first pass.")
    speak_error: list[Exception] = []

    def speak() -> None:
        try:
            result = dependencies.caller("speak", {"message": CALIBRATION_SCRIPT})
            if inspect.isawaitable(result):
                asyncio.run(result)
        except Exception as exc:  # reported, never raised into the measuring thread
            speak_error.append(exc)

    speaker = threading.Thread(target=speak, daemon=True)
    speaker.start()
    time.sleep(0.6)  # let playback actually start before the window opens
    bleed = _measure_input(seconds)
    speaker.join(timeout=max(5.0, seconds))

    if speak_error:
        raise click.ClickException(
            f"Could not play the calibration script through the runtime: {speak_error[0]}"
        )

    click.echo("")
    click.echo(f"Now speak normally for {seconds:.0f} seconds, at your usual distance.")
    if countdown is None:
        click.pause(info="Press any key when you are ready to talk...")
    elif countdown > 0:
        # An agent driving this has no keyboard to offer, so the wait has to be
        # a clock. Spoken so the user hears it start even while looking away.
        click.echo(f"Starting in {countdown:.0f} seconds — start talking on 'go'.")
        _announce(dependencies, f"Calibrating. Start talking in {countdown:.0f} seconds.")
        time.sleep(countdown)
        click.echo("Go.")
    voice = _measure_input(seconds)

    bleed_armed_p90 = bleed.p90_dbfs + duck_offset_db
    gap_db = voice.median_dbfs - bleed_armed_p90
    usable = gap_db >= safety_db + 6.0
    # The gate enforces a rise above the floor, and during playback the floor is
    # the bleed — so the rise that separates the user from Kokoro is the gap less
    # a safety band. It has to be applied to VOX_BARGE_IN_VAD_MARGIN_DB, the
    # *floor* of the required rise: the rise is
    # clamp(k * spread, vad_margin, max_vad_margin), so the floor is the only
    # value that guarantees a separation. This used to recommend the ceiling
    # instead, which was backwards in the case that matters — a small gap
    # produced a small number, and lowering the ceiling loosens the gate, so
    # following the advice on speakers would have made Kokoro interrupt itself
    # more readily. The ceiling is only raised here so it cannot sit below the
    # floor, which CaptureConfig rejects outright.
    recommended = round(gap_db - safety_db, 1) if usable else None
    ceiling = max(24.0, recommended) if recommended is not None else None

    return {
        "device": voice.device,
        "duck_volume": duck,
        "bleed": {
            "frames": bleed.frames,
            "median_dbfs": round(bleed.median_dbfs, 1),
            "p90_dbfs": round(bleed.p90_dbfs, 1),
            "peak_dbfs": round(bleed.peak_dbfs, 1),
            "p90_dbfs_at_armed_volume": round(bleed_armed_p90, 1),
        },
        "voice": {
            "frames": voice.frames,
            "median_dbfs": round(voice.median_dbfs, 1),
            "p90_dbfs": round(voice.p90_dbfs, 1),
            "peak_dbfs": round(voice.peak_dbfs, 1),
        },
        "gap_db": round(gap_db, 1),
        # No numbers when the hardware cannot support barge-in: there is no
        # setting that manufactures a gap, and printing one invites someone to
        # apply it and conclude the feature is broken rather than impossible.
        "recommended": (
            {
                "VOX_BARGE_IN_VAD_MARGIN_DB": recommended,
                "VOX_BARGE_IN_MAX_VAD_MARGIN_DB": ceiling,
                "VOX_BARGE_IN_SPEECH_MARGIN_DB": recommended,
            }
            if usable
            else None
        ),
        # `vox set` writes ~/.vox/settings.json and restarts the runtime.
        # launchctl setenv never reaches it — launchd hands the daemon none of
        # that environment, so anything set that way is silently inert.
        "apply": (
            f"vox set VOX_BARGE_IN_VAD_MARGIN_DB={recommended} "
            f"VOX_BARGE_IN_MAX_VAD_MARGIN_DB={ceiling} "
            f"VOX_BARGE_IN_SPEECH_MARGIN_DB={recommended}"
            if usable
            else None
        ),
        "usable": usable,
        "verdict": (
            f"Your voice sits {gap_db:.1f} dB above the loudest bleed at armed "
            f"volume. Require a {recommended} dB rise."
            if usable
            else (
                f"Only {gap_db:.1f} dB separates your voice from the playback bleed — too "
                "little to gate on reliably, and no setting invents the difference. "
                "Expect Kokoro to interrupt itself. Use headphones, or leave barge-in off "
                "and interrupt with the Reply button / control-option-command-R, which "
                "needs no acoustics at all."
            )
        ),
    }


def _invoke_and_print(
    dependencies: _Dependencies,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> None:
    try:
        result = dependencies.caller(tool_name, arguments)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
    except Exception as exc:
        raise click.ClickException(
            f"Vox runtime call failed at {MCP_URL}: {exc}. "
            "Run `vox start-runtime` to check the installed runtime."
        ) from exc
    _print_json(result)


def _extract_tool_result(result: Any) -> Any:
    if bool(getattr(result, "isError", False)):
        raise MCPCallError(_content_error(result) or "MCP tool returned an error")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured

    content = list(getattr(result, "content", []) or [])
    text_items = [item.text for item in content if hasattr(item, "text")]
    if len(text_items) == 1:
        try:
            return json.loads(text_items[0])
        except (json.JSONDecodeError, TypeError):
            return {"text": text_items[0]}
    if text_items:
        return {"content": text_items}
    return {"content": [_jsonable(item) for item in content]}


def _content_error(result: Any) -> str:
    return "\n".join(
        str(item.text)
        for item in (getattr(result, "content", []) or [])
        if hasattr(item, "text") and str(item.text).strip()
    )


def _print_json(value: Any) -> None:
    click.echo(
        json.dumps(
            _jsonable(value),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json", by_alias=True))
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def _drop_none(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _command_output(result: subprocess.CompletedProcess[str]) -> str:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    return stderr or stdout


cli = create_cli()


def main() -> None:
    cli(prog_name="vox")


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "HEALTH_URL",
    "InstalledRuntimeStarter",
    "MCPToolCaller",
    "MCP_URL",
    "cli",
    "create_cli",
    "main",
]
