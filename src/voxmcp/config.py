"""Configuration with a hard local-only network boundary."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .errors import ConfigurationError


DEFAULT_STT_URL = "http://127.0.0.1:2022/v1"
DEFAULT_TTS_URL = "http://127.0.0.1:8880/v1"


def validate_loopback_url(value: str, *, name: str = "URL") -> str:
    """Validate and normalize an HTTP(S) URL with a literal loopback host.

    Only ``localhost``, IPv4 addresses in ``127.0.0.0/8``, and ``::1`` are
    accepted.  Hostnames that merely *resolve* to loopback are deliberately
    rejected: DNS and hosts-file changes must not be able to turn a local-only
    configuration into an outbound request.
    """

    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty URL")

    try:
        parsed = urlsplit(value.strip())
        port = parsed.port  # Access validates malformed and out-of-range ports.
    except ValueError as exc:
        raise ConfigurationError(f"{name} is not a valid URL: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ConfigurationError(f"{name} must use http or https")
    if not parsed.netloc or parsed.hostname is None:
        raise ConfigurationError(f"{name} must include a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(f"{name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError(f"{name} must not contain a query or fragment")

    host = parsed.hostname.lower()
    if "%" in host:
        raise ConfigurationError(f"{name} must not use an IPv6 scope identifier")
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ConfigurationError(
                f"{name} host must be localhost or a literal loopback address"
            ) from exc
        if not address.is_loopback:
            raise ConfigurationError(f"{name} host is not loopback: {host}")

    if ":" in host:
        normalized_host = f"[{host}]"
    else:
        normalized_host = host
    if port is not None:
        normalized_host = f"{normalized_host}:{port}"

    path = parsed.path.rstrip("/")
    normalized = SplitResult(scheme, normalized_host, path, "", "")
    return urlunsplit(normalized)


def _default_state_dir() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state).expanduser() / "voxmcp"
    return Path.home() / ".local" / "state" / "voxmcp"


@dataclass(frozen=True, slots=True)
class VoxConfig:
    """Safe defaults for the local voice daemon and thin MCP adapters."""

    stt_url: str = DEFAULT_STT_URL
    tts_url: str = DEFAULT_TTS_URL
    state_dir: Path = field(default_factory=_default_state_dir)
    idle_timeout_seconds: float = 600.0
    tool_timeout_seconds: float = 600.0
    privacy_enabled: bool = True
    persist_transcripts: bool = False
    persist_audio: bool = True
    event_log_filename: str = "events.jsonl"
    local_only: bool = True
    # Consent-adjacent, so it lives here and surfaces in diagnostics(privacy)
    # rather than hiding among the numeric tuning knobs: when armed, the
    # microphone is physically open while the agent is still speaking.
    barge_in_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.local_only:
            raise ConfigurationError("Vox MCP cannot disable local-only mode")
        object.__setattr__(
            self, "stt_url", validate_loopback_url(self.stt_url, name="STT URL")
        )
        object.__setattr__(
            self, "tts_url", validate_loopback_url(self.tts_url, name="TTS URL")
        )
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser())

        if self.idle_timeout_seconds < 0:
            raise ConfigurationError("idle_timeout_seconds cannot be negative")
        if self.tool_timeout_seconds <= 0:
            raise ConfigurationError("tool_timeout_seconds must be positive")
        if not isinstance(self.privacy_enabled, bool):
            raise ConfigurationError("privacy_enabled must be a boolean")
        if not isinstance(self.persist_transcripts, bool):
            raise ConfigurationError("persist_transcripts must be a boolean")
        if not isinstance(self.persist_audio, bool):
            raise ConfigurationError("persist_audio must be a boolean")
        if not isinstance(self.barge_in_enabled, bool):
            raise ConfigurationError("barge_in_enabled must be a boolean")

        filename = self.event_log_filename
        if (
            not filename
            or filename in {".", ".."}
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
        ):
            raise ConfigurationError("event_log_filename must be a simple filename")

    @property
    def snapshot_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def event_log_path(self) -> Path:
        return self.state_dir / self.event_log_filename

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "VoxConfig":
        """Load configuration from a supplied mapping or ``os.environ``."""

        env = os.environ if environ is None else environ
        return cls(
            stt_url=env.get("VOX_STT_URL", DEFAULT_STT_URL),
            tts_url=env.get("VOX_TTS_URL", DEFAULT_TTS_URL),
            state_dir=Path(env["VOX_STATE_DIR"]) if "VOX_STATE_DIR" in env else _default_state_dir(),
            idle_timeout_seconds=_env_float(env, "VOX_IDLE_TIMEOUT_SECONDS", 600.0),
            tool_timeout_seconds=_env_float(env, "VOX_TOOL_TIMEOUT_SECONDS", 600.0),
            privacy_enabled=_env_bool(env, "VOX_PRIVACY_ENABLED", True),
            persist_transcripts=_env_bool(env, "VOX_PERSIST_TRANSCRIPTS", False),
            persist_audio=_env_bool(env, "VOX_PERSIST_AUDIO", True),
            event_log_filename=env.get("VOX_EVENT_LOG_FILENAME", "events.jsonl"),
            barge_in_enabled=_env_bool(env, "VOX_BARGE_IN_ENABLED", False),
        )


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a number") from exc


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{key} must be true or false")
