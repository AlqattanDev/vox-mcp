"""FastMCP surfaces for the persistent Vox daemon and stdio fallback.

The Streamable HTTP and stdio entry points expose the same compact tool set.
All conversational ownership is derived from FastMCP's request context; a
model cannot impersonate another client by supplying its own owner id.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import __version__
from .audio import default_latest_recording_path
from .errors import VoxError
from .lease import DEFAULT_AGENT


_ENGINE: Any | None = None
_ENGINE_LOCK = threading.Lock()
_AGENT_MAX_LENGTH = 64
# Strong references to fire-and-forget note captures so the loop cannot GC them.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def get_engine() -> Any:
    """Lazily construct the one process-local engine."""

    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                from .engine import VoxEngine

                _ENGINE = VoxEngine.default()
    return _ENGINE


def _client_id(ctx: Context) -> str:
    """Return a reconnect-stable identity for the calling MCP host.

    Streamable HTTP session IDs change whenever Claude Code, Codex, or the Vox
    CLI reconnects.  The initialize-time client name is stable across those
    reconnects and across host upgrades, so leases follow the host instead of
    trapping it behind its own stale session.
    """

    explicit = ctx.client_id
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    request_context = ctx.request_context
    session = getattr(request_context, "session", None)
    params = getattr(session, "client_params", None)
    implementation = getattr(params, "clientInfo", None)
    name = getattr(implementation, "name", None)
    if name is not None and str(name).strip():
        normalized = re.sub(r"[^a-z0-9._-]+", "-", str(name).strip().lower()).strip("-")
        if normalized:
            return f"mcp:host:{normalized[:96]}"
    return f"mcp:{ctx.session_id}"


def _normalize_agent(value: Any) -> str | None:
    """Reduce a speaker label to the safe alphabet, or None if nothing is left."""

    if value is None:
        return None
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value).strip().lower()).strip("-")
    return normalized[:_AGENT_MAX_LENGTH] or None


def _agent_from_url(ctx: Context) -> str | None:
    """Read ?agent=<name> off the MCP URL for this request.

    This is the primary mechanism: a project sets it once in its own MCP
    config, so no agent can forget to identify itself per call. stdio has no
    request, hence the defensive lookups.
    """

    request = getattr(ctx.request_context, "request", None)
    params = getattr(request, "query_params", None)
    if params is None:
        return None
    try:
        return params.get("agent")
    except Exception:
        return None


def _agent_id(ctx: Context, explicit: str | None = None) -> str:
    """Resolve who is speaking, independent of which host holds the lease.

    Two Claude Code windows in different projects share one _client_id, so the
    lease cannot tell them apart. The speaker label can, which is what lets
    them queue and sound different.
    """

    return _normalize_agent(_agent_from_url(ctx)) or _normalize_agent(explicit) or DEFAULT_AGENT


def _drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


async def _invoke(engine: Any, method_name: str, **kwargs: Any) -> Any:
    method = getattr(engine, method_name, None)
    if method is None or not callable(method):
        raise RuntimeError(f"Vox engine does not implement {method_name}")
    try:
        result = method(**_drop_none(kwargs))
        if inspect.isawaitable(result):
            result = await result
        return _jsonable(result)
    except VoxError as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "recoverable": True,
        }


def _jsonable(value: Any) -> Any:
    """Normalize engine dataclasses/enums without losing voice transcripts."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict(include_transcript=True))
        except TypeError:
            return _jsonable(to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def _redact_sensitive(value: Any) -> Any:
    normalized = _jsonable(value)
    if isinstance(normalized, dict):
        redacted: dict[str, Any] = {}
        for key, item in normalized.items():
            lowered = key.lower()
            if any(marker in lowered for marker in ("token", "secret", "password", "api_key")):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(normalized, list):
        return [_redact_sensitive(item) for item in normalized]
    return normalized


def _loopback_client(client: Any) -> bool:
    if client is None:
        return False
    host = str(getattr(client, "host", "")).strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _loopback_peer(request: Request) -> bool:
    return _loopback_client(request.client)


# The one place a Vox surface is reachable from off this machine. It is the
# phone standing in for the local devices, so it cannot be loopback-only — but
# it is not the open internet either: `tailscale serve` hands the request over
# with the peer's tailnet address, and every tailnet address lives in the CGNAT
# range below. Anything outside it never reaches the socket, and inside it the
# control token is still required.
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _phone_peer_allowed(client: Any) -> bool:
    if _loopback_client(client):
        return True
    if client is None:
        return False
    try:
        address = ipaddress.ip_address(str(getattr(client, "host", "")).strip())
    except ValueError:
        return False
    return address in _TAILNET_V4 or address in _TAILNET_V6


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _control_token_from_request(request: Request) -> str | None:
    """Accept the native menu-bar header and standard Bearer authentication."""

    native = request.headers.get("x-vox-token", "").strip()
    return native or _bearer_token(request)


def _listen_bounds(listen_duration_max: float, listen_duration_min: float) -> None:
    if not 0 < listen_duration_max <= 300:
        raise ValueError("listen_duration_max must be in (0, 300]")
    if not 0 <= listen_duration_min <= listen_duration_max:
        raise ValueError("listen_duration_min must be between 0 and listen_duration_max")


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
LOCAL_ACTION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def create_mcp(engine: Any | None = None, *, control_token: str | None = None) -> FastMCP:
    """Create an isolated server, optionally backed by an injected engine."""

    @asynccontextmanager
    async def runtime_lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            runtime = engine if engine is not None else get_engine()
            shutdown = getattr(runtime, "shutdown", None)
            if callable(shutdown):
                result = shutdown()
                if inspect.isawaitable(result):
                    await result

    server = FastMCP(
        "vox",
        version=__version__,
        instructions=(
            "Vox is local-only voice I/O. Start an explicit voice session before "
            "listening. The microphone is permitted only during listen/converse; "
            "cancel ends the current turn, while stop ends the session.\n"
            "Voice turn contract: a spoken turn is a conversation, not a report. "
            "Keep spoken messages to one or two sentences and do not call other "
            "tools between conversation turns — the user is sitting in silence "
            "while you work, and that silence is the slowest part of voice mode. "
            "Put detail on screen, never in the speaker."
        ),
        mask_error_details=True,
        strict_input_validation=True,
        lifespan=runtime_lifespan,
    )
    token_was_explicit = control_token is not None
    configured_token = (
        control_token.strip()
        if control_token is not None
        else os.environ.get("VOX_CONTROL_TOKEN", "").strip()
    )

    def current_engine() -> Any:
        return engine if engine is not None else get_engine()

    def effective_control_tokens() -> tuple[str, ...]:
        """Resolve accepted tokens without exposing them through MCP resources."""

        # An explicitly supplied value is authoritative, including an empty
        # value used by embedders to fail the route closed.
        if token_was_explicit:
            return (configured_token,) if configured_token else ()
        values: list[str] = []
        ensure = getattr(current_engine(), "ensure_control_token", None)
        if callable(ensure):
            try:
                generated = str(ensure()).strip()
            except OSError:
                generated = ""
            if generated:
                values.append(generated)
        # Retain VOX_CONTROL_TOKEN as a headless-client fallback. The private
        # file token remains valid so the native menu bar never disagrees with
        # the daemon when both mechanisms are configured.
        if configured_token and configured_token not in values:
            values.append(configured_token)
        return tuple(values)

    async def dispatch_control(
        action: str,
        client_id: str,
        *,
        seconds: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Any:
        options = dict(extra or {})
        if action == "note":
            return await _invoke(
                current_engine(),
                "note",
                client_id=client_id,
                **options,
            )
        if action == "reply":
            # A user-initiated reply, auto-addressed to the last voice heard.
            return await _invoke(
                current_engine(),
                "reply",
                client_id=client_id,
                **options,
            )
        if action == "read_aloud":
            return await _invoke(
                current_engine(),
                "read_aloud",
                client_id=client_id,
                **options,
            )
        if action in {"dictate_start", "dictate_end"}:
            return await _invoke(
                current_engine(),
                "dictate",
                action=action,
                client_id=client_id,
                **options,
            )
        if action in {"cancel", "repeat", "status", "deliver_text", "gate_open", "gate_close"}:
            return await _invoke(
                current_engine(),
                "control",
                action=action,
                client_id=client_id,
                **options,
            )
        if action == "end_turn":
            return await _invoke(
                current_engine(),
                "control",
                action="manual_end",
                client_id=client_id,
                **options,
            )
        if action == "wait":
            return await _invoke(
                current_engine(),
                "session",
                action="pause",
                client_id=client_id,
                pause_seconds=seconds,
                **options,
            )
        if action in {"start", "stop", "pause", "mute", "cycle_mode", "set_mode", "mode"}:
            return await _invoke(
                current_engine(),
                "session",
                action=action,
                client_id=client_id,
                pause_seconds=seconds,
                **options,
            )
        if action in {"resume", "unmute"}:
            return await _invoke(
                current_engine(),
                "session",
                action="resume",
                client_id=client_id,
                **options,
            )
        raise ValueError(f"Unsupported control action: {action}")

    @server.tool(
        name="converse",
        description=(
            "Speak a message locally and optionally listen for one response. "
            "Speak one or two sentences, not a report: do not call other tools between "
            "conversation turns, and put detail on screen rather than in the spoken message."
        ),
        annotations=LOCAL_ACTION,
    )
    async def converse(
        ctx: Context,
        message: str,
        wait_for_response: bool = True,
        voice: str | None = None,
        speed: float | None = None,
        listen_duration_max: float = 75.0,
        listen_duration_min: float = 0.5,
        trailing_silence_s: float = 1.6,
        onset_timeout: float | None = None,
        agent: str | None = None,
    ) -> Any:
        _listen_bounds(listen_duration_max, listen_duration_min)
        return await _invoke(
            current_engine(),
            "converse",
            client_id=_client_id(ctx),
            agent=_agent_id(ctx, agent),
            message=message,
            wait_for_response=wait_for_response,
            voice=voice,
            speed=speed,
            listen_duration_max=listen_duration_max,
            listen_duration_min=listen_duration_min,
            trailing_silence_s=trailing_silence_s,
            onset_timeout=onset_timeout,
        )

    @server.tool(
        name="speak",
        description="Speak local TTS without opening the microphone.",
        annotations=LOCAL_ACTION,
    )
    async def speak(
        ctx: Context,
        message: str,
        voice: str | None = None,
        speed: float | None = None,
        interruptible: bool = True,
        agent: str | None = None,
    ) -> Any:
        return await _invoke(
            current_engine(),
            "speak",
            client_id=_client_id(ctx),
            agent=_agent_id(ctx, agent),
            message=message,
            voice=voice,
            speed=speed,
            interruptible=interruptible,
        )

    @server.tool(
        name="listen",
        description="Open the microphone for one bounded utterance, then close it.",
        annotations=LOCAL_ACTION,
    )
    async def listen(
        ctx: Context,
        listen_duration_max: float = 75.0,
        listen_duration_min: float = 0.5,
        trailing_silence_s: float = 1.6,
        onset_timeout: float | None = None,
        language: str | None = None,
        agent: str | None = None,
    ) -> Any:
        _listen_bounds(listen_duration_max, listen_duration_min)
        return await _invoke(
            current_engine(),
            "listen",
            client_id=_client_id(ctx),
            agent=_agent_id(ctx, agent),
            listen_duration_max=listen_duration_max,
            listen_duration_min=listen_duration_min,
            trailing_silence_s=trailing_silence_s,
            onset_timeout=onset_timeout,
            language=language,
        )

    @server.tool(
        name="voice_session",
        description=(
            "Shared voice session controls: start/stop/pause, status, io mode "
            "(talk|narrate|dictate), claim undelivered transcript. Session is "
            "shared — no exclusive owner; audio queues FIFO. handoff/takeover "
            "are legacy no-ops."
        ),
        annotations=LOCAL_ACTION,
    )
    async def voice_session(
        ctx: Context,
        action: Literal[
            "start",
            "stop",
            "pause",
            "resume",
            "mute",
            "unmute",
            "status",
            "handoff",
            "takeover",
            "set_mode",
            "cycle_mode",
            "claim_undelivered",
        ],
        seconds: float | None = None,
        target: str | None = None,
        force: bool = False,
        agent: str | None = None,
        mode: str | None = None,
    ) -> Any:
        mapped_action = "resume" if action == "unmute" else action
        return await _invoke(
            current_engine(),
            "session",
            action=mapped_action,
            client_id=_client_id(ctx),
            agent=_agent_id(ctx, agent),
            pause_seconds=seconds,
            target_client_id=target,
            force=force,
            mode=mode,
        )

    @server.tool(
        name="voice_control",
        description=(
            "Control the active turn: cancel, end, repeat, wait, mute, resume, or "
            "deliver_text. deliver_text ends an in-flight listen and makes the "
            "supplied text the user's turn, so a typed message is not stuck behind "
            "a microphone they chose not to use."
        ),
        annotations=LOCAL_ACTION,
    )
    async def voice_control(
        ctx: Context,
        action: Literal[
            "cancel", "end_turn", "repeat", "wait", "mute", "unmute", "resume",
            "status", "deliver_text",
        ],
        seconds: float | None = None,
        text: str | None = None,
    ) -> Any:
        return await dispatch_control(
            action,
            _client_id(ctx),
            seconds=seconds,
            extra={"text": text} if text is not None else None,
        )

    @server.tool(
        name="service",
        description="Inspect or manage the local Whisper, Kokoro, or Vox service.",
        annotations=LOCAL_ACTION,
    )
    async def service(
        ctx: Context,
        action: Literal["status", "health", "start", "stop", "restart", "logs"] = "status",
        service_name: Literal["all", "whisper", "kokoro", "vox"] = "all",
        lines: int = 80,
    ) -> Any:
        mapped_action = "status" if action == "health" else action
        return await _invoke(
            current_engine(),
            "service",
            action=mapped_action,
            service_name=service_name,
            lines=lines,
            client_id=_client_id(ctx),
        )

    @server.tool(
        name="diagnostics",
        description="Return local-only device, service, dependency, or privacy diagnostics.",
        annotations=READ_ONLY,
    )
    async def diagnostics(
        ctx: Context,
        section: Literal["all", "audio", "services", "dependencies", "privacy", "events"] = "all",
        include_logs: bool = False,
    ) -> Any:
        return await _invoke(
            current_engine(),
            "diagnostics",
            section=section,
            include_logs=include_logs,
            client_id=_client_id(ctx),
        )

    @server.tool(
        name="transcribe",
        description="Transcribe one local audio file with the local Whisper service.",
        annotations=READ_ONLY,
    )
    async def transcribe(
        ctx: Context,
        audio_file: str | None = None,
        latest: bool = False,
        language: str | None = None,
        output_format: Literal["text", "json", "srt", "vtt", "csv"] = "text",
        word_timestamps: bool = False,
    ) -> Any:
        return await _invoke(
            current_engine(),
            "transcribe",
            path=audio_file,
            latest=True if latest else None,
            language=language,
            output_format=output_format,
            word_timestamps=word_timestamps,
            client_id=_client_id(ctx),
        )

    @server.tool(
        name="voice_registry",
        description="List local TTS voices and their provider metadata.",
        annotations=READ_ONLY,
    )
    async def voice_registry(ctx: Context, provider: str | None = None) -> Any:
        return await _invoke(
            current_engine(),
            "voice_registry",
            provider=provider,
            client_id=_client_id(ctx),
        )

    @server.tool(
        name="companion",
        description=(
            "Hand the conversation to a fast companion voice while you work, so the user "
            "is not left in silence. Call this BEFORE a long stretch of tool use, with a "
            "brief saying what you are about to do. The companion holds small talk only "
            "and escalates anything about the user's project straight back to you; the "
            "returned transcript is everything it heard. Off unless VOX_COMPANION_ENABLED."
        ),
        annotations=LOCAL_ACTION,
    )
    async def companion(
        ctx: Context,
        brief: str,
        budget_turns: int = 6,
    ) -> Any:
        return await _invoke(
            current_engine(),
            "companion",
            client_id=_client_id(ctx),
            action="handoff",
            brief=brief,
            budget_turns=budget_turns,
        )

    @server.tool(
        name="voice_survey",
        description="Run or record a local voice-comparison survey.",
        annotations=LOCAL_ACTION,
    )
    async def voice_survey(
        ctx: Context,
        turns: list[dict[str, Any]],
        agent: str | None = None,
    ) -> Any:
        # `agent` reads that agent's registered voice and uses it for the whole
        # run, which is how a scripted interview is read in the companion voice
        # instead of the asking agent's. The engine always supported it; the tool
        # simply never passed it, so the documented use case was unreachable.
        return await _invoke(
            current_engine(),
            "survey",
            turns=turns,
            agent=agent,
            client_id=_client_id(ctx),
        )

    @server.tool(
        name="exchange_history",
        description="Read privacy-filtered recent voice exchange history and statistics.",
        annotations=READ_ONLY,
    )
    async def exchange_history(
        ctx: Context,
        action: Literal["recent", "summary", "stats", "conversations"] = "recent",
        limit: int = 20,
        days: int = 7,
    ) -> Any:
        mapped_action = {
            "recent": "list",
            "summary": "list",
            "stats": "stats",
            "conversations": "conversations",
        }[action]
        return await _invoke(
            current_engine(),
            "exchange_history",
            action=mapped_action,
            limit=limit,
            days=days,
            client_id=_client_id(ctx),
        )

    @server.resource(
        "voice://status",
        name="voice_status",
        description="Current Vox session, lease, microphone, and backend state.",
        mime_type="application/json",
    )
    async def status_resource() -> Any:
        return await _invoke(current_engine(), "status")

    @server.resource(
        "voice://voices",
        name="voices",
        description="All available local TTS voices.",
        mime_type="application/json",
    )
    async def voices_resource(ctx: Context) -> Any:
        return await _invoke(current_engine(), "voice_registry", client_id=_client_id(ctx))

    @server.resource(
        "voice://config",
        name="voice_config",
        description="Effective local-only Vox configuration with secrets redacted.",
        mime_type="application/json",
    )
    async def config_resource() -> Any:
        effective = getattr(current_engine(), "config", {})
        return _redact_sensitive(effective)

    @server.resource(
        "voice://diagnostics",
        name="voice_diagnostics",
        description="Current local device and service diagnostics.",
        mime_type="application/json",
    )
    async def diagnostics_resource(ctx: Context) -> Any:
        return await _invoke(
            current_engine(),
            "diagnostics",
            section="all",
            include_logs=False,
            client_id=_client_id(ctx),
        )

    @server.resource(
        "voice://audio/latest",
        name="latest_audio",
        description="The single rolling local recovery recording, when present.",
        mime_type="audio/wav",
    )
    async def latest_audio_resource() -> bytes:
        active_engine = current_engine()
        store = getattr(active_engine, "store", None)
        candidate = getattr(store, "latest_stt", None)
        if candidate is None:
            candidate = getattr(active_engine, "latest_audio_path", None)
        if candidate is None:
            audio = getattr(active_engine, "audio", None)
            candidate = getattr(audio, "latest_wav_path", None)
        path = Path(candidate) if candidate is not None else default_latest_recording_path()
        if not path.is_file():
            raise FileNotFoundError("No recoverable voice recording is available")
        return path.read_bytes()

    @server.prompt(
        name="voice_mode",
        description="Host-neutral operating instructions for a Vox voice session.",
    )
    def voice_mode_prompt() -> str:
        return (
            "Use voice_session(action='start') when the user explicitly asks for voice mode. "
            "Use converse for normal turns and speak for narration that must not open the mic. "
            "The microphone listens only during listen/converse. Treat cancel as current-turn "
            "cancellation, not session shutdown. Use pause or wait when the user needs time, "
            "and voice_session(action='stop') only for an explicit stop or session end. Keep a "
            "visible verbatim transcript unless the user opts out.\n"
            "\n"
            "Voice turn contract. The microphone and the speaker are fast; the pause between "
            "turns is you. Honour these while a voice session is live:\n"
            "- Speak one or two sentences. Answer first, then stop. Length is latency.\n"
            "- Do not call other tools between conversation turns. Finish investigating before "
            "you open the mic, not after.\n"
            "- Never read tables, code, file paths, or lists aloud. Say the conclusion; leave the "
            "detail on screen where it can be read.\n"
            "- If you must go and work, say so first — speak('reading the audio path now, one "
            "minute') beats going silent. Unexplained silence reads as a crash.\n"
            "- Use low reasoning effort for conversational turns. Save deep thinking for the "
            "work itself, not for deciding what to say back.\n"
            "- For status updates and sign-offs pass wait_for_response=false. Do not make the "
            "user confirm that you finished."
        )

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health_route(request: Request) -> Response:
        if not _loopback_peer(request):
            return JSONResponse({"healthy": False, "error": "loopback_required"}, status_code=403)
        # ?levels_since=N returns only the waveform samples newer than the ones
        # this caller already drew. Absent or unparseable means "give me the whole
        # window", which is what a reader that has just started wants.
        try:
            levels_since = max(0, int(request.query_params.get("levels_since", "0")))
        except (TypeError, ValueError):
            levels_since = 0
        try:
            tts_levels_since = max(0, int(request.query_params.get("tts_levels_since", "0")))
        except (TypeError, ValueError):
            tts_levels_since = 0
        try:
            payload = await _invoke(
                current_engine(),
                "health",
                levels_since=levels_since,
                tts_levels_since=tts_levels_since,
            )
        except Exception:
            return JSONResponse({"healthy": False, "error": "health_check_failed"}, status_code=503)
        healthy = not (isinstance(payload, dict) and payload.get("healthy") is False)
        return JSONResponse(payload, status_code=200 if healthy else 503)

    @server.custom_route("/control", methods=["POST"], include_in_schema=False)
    async def control_route(request: Request) -> Response:
        if not _loopback_peer(request):
            return JSONResponse({"ok": False, "error": "loopback_required"}, status_code=403)
        expected_tokens = effective_control_tokens()
        if not expected_tokens:
            return JSONResponse({"ok": False, "error": "control_token_not_configured"}, status_code=503)
        supplied = _control_token_from_request(request)
        if supplied is None or not any(
            secrets.compare_digest(supplied, expected) for expected in expected_tokens
        ):
            return JSONResponse(
                {"ok": False, "error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "object_required"}, status_code=400)
        action = body.get("action")
        allowed = {
            "start",
            "cancel",
            "end_turn",
            "stop",
            "pause",
            "resume",
            "mute",
            "unmute",
            "status",
            "cycle_mode",
            "set_mode",
            "repeat",
            "note",
            "reply",
            "deliver_text",
            "gate_open",
            "gate_close",
            "dictate_start",
            "dictate_end",
            "read_aloud",
        }
        if action not in allowed:
            return JSONResponse({"ok": False, "error": "unsupported_action"}, status_code=400)
        options = {key: value for key, value in body.items() if key not in {"action", "client_id"}}
        if action in {"note", "reply"}:
            # A note/reply capture can run up to the utterance cap — far longer
            # than a control request should block. Kick it off and return at
            # once; the mic earcon and red glyph tell the user to speak, and the
            # transcript lands in undelivered_heard for the agent's next turn.
            task = asyncio.create_task(dispatch_control(action, "http-control", extra=options))
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
            return JSONResponse({"ok": True, "result": {"status": "listening"}}, status_code=202)
        try:
            payload = await dispatch_control(
                action,
                "http-control",
                seconds=options.pop("seconds", None),
                extra=options,
            )
        except Exception:
            return JSONResponse({"ok": False, "error": "control_failed"}, status_code=500)
        return JSONResponse({"ok": True, "result": payload})

    def _phone_token_ok(supplied: str | None) -> bool:
        expected_tokens = effective_control_tokens()
        if not expected_tokens or not supplied:
            return False
        # Whitespace anywhere in the token is dropped, not just at the ends.
        # The token reaches the phone by being copied out of a terminal, and a
        # terminal wraps: the copy comes back with the wrap turned into spaces
        # in the middle of the string. A token is base64url and can never
        # legitimately contain a space, so there is nothing to lose by
        # forgiving it — and the alternative is a "connection failed" whose
        # cause is invisible on the phone.
        cleaned = "".join(supplied.split())
        if not cleaned:
            return False
        return any(secrets.compare_digest(cleaned, expected) for expected in expected_tokens)

    @server.custom_route("/phone", methods=["GET"], include_in_schema=False)
    async def phone_route(request: Request) -> Response:
        # The page carries no secret of its own; the token is checked when the
        # socket opens. Serving it unauthenticated keeps the token out of a URL
        # that would otherwise sit in the phone's history and autocomplete.
        if not _phone_peer_allowed(request.client):
            return Response("not reachable from this network", status_code=403)
        from .phone_client import PHONE_HTML

        return Response(
            PHONE_HTML,
            media_type="text/html; charset=utf-8",
            headers={"cache-control": "no-store"},
        )

    async def phone_socket(websocket: WebSocket) -> None:
        """One phone, acting as this machine's microphone and speaker."""

        from .remote import PHONE

        if not _phone_peer_allowed(websocket.client):
            await websocket.close(code=1008)
            return
        if not _phone_token_ok(websocket.query_params.get("t", "").strip()):
            await websocket.close(code=1008)
            return

        await websocket.accept()
        loop = asyncio.get_running_loop()
        outbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def enqueue(message: dict[str, Any]) -> None:
            outbound.put_nowait(message)

        info = PHONE.attach(
            send=enqueue,
            loop=loop,
            user_agent=websocket.headers.get("user-agent", ""),
        )

        async def pump_outbound() -> None:
            while True:
                message = await outbound.get()
                if message is None:
                    return
                await websocket.send_json(message)

        writer = asyncio.create_task(pump_outbound())

        async def read_aloud_for_phone(
            text: str, *, say_id: str | None, replace: bool
        ) -> None:
            """Read one selection to the phone, and say when it is really over.

            A long selection is split into spans here and played one at a
            time, each released by the phone's own ``ended``. Only this side
            knows there is another span coming, so a phone that opened a
            socket purely to be the speaker has no way to tell "this wav
            finished" from "the reading finished" — it left after the first
            span and the rest was spoken into the room. ``say_done`` is that
            missing sentence.
            """

            if replace:
                # A second tap replaces the first rather than queueing behind
                # it: the user asked for this message, not for both in a row.
                await dispatch_control("cancel", "phone")
            try:
                await dispatch_control("read_aloud", "phone", extra={"text": text})
            finally:
                done: dict[str, Any] = {"type": "say_done"}
                if say_id:
                    done["id"] = say_id
                PHONE.notify(done)

        try:
            while True:
                message = await websocket.receive()
                kind = message.get("type")
                if kind == "websocket.disconnect":
                    break
                payload = message.get("bytes")
                if payload:
                    # The realtime path: int16 mono PCM straight into the fan-out.
                    PHONE.push_pcm(payload)
                    continue
                text = message.get("text")
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "ended":
                    playback_id = str(event.get("id", ""))
                    if playback_id:
                        PHONE.finish_playback(playback_id)
                elif event.get("type") == "say":
                    # The phone asking for a message read back verbatim. It
                    # rides the existing read_aloud path — straight to Kokoro,
                    # no model anywhere that could paraphrase — and the wav
                    # comes back over this same socket as a normal play,
                    # because an attached phone is already the speaker.
                    spoken = str(event.get("text", "")).strip()[:4000]
                    if spoken:
                        task = asyncio.create_task(
                            read_aloud_for_phone(
                                spoken,
                                say_id=str(event.get("id", "")) or None,
                                replace=event.get("replace") is True,
                            )
                        )
                        _BACKGROUND_TASKS.add(task)
                        task.add_done_callback(_BACKGROUND_TASKS.discard)
                elif event.get("type") == "control":
                    # The phone as the turn key, not only as the microphone.
                    #
                    # Until this existed the phone could not begin or end a
                    # turn: the mic opened only when something on the Mac
                    # asked, so "voice on" in the app meant no more than
                    # "available", and there was no way to stop a turn once it
                    # had started. These three are the whole of it —
                    # gate_open starts (or joins) a turn, gate_close ends it
                    # and submits what was said, cancel throws it away. Every
                    # other control stays on the Mac, where the agent is.
                    action = str(event.get("action", ""))
                    if action in {"gate_open", "gate_close", "cancel"}:
                        task = asyncio.create_task(dispatch_control(action, "phone"))
                        _BACKGROUND_TASKS.add(task)
                        task.add_done_callback(_BACKGROUND_TASKS.discard)
        except WebSocketDisconnect:
            pass
        finally:
            PHONE.detach(info.connection_id)
            outbound.put_nowait(None)
            writer.cancel()

    server._additional_http_routes.append(WebSocketRoute("/phone/ws", endpoint=phone_socket))

    return server


mcp = create_mcp()


def run_stdio(server: FastMCP | None = None) -> None:
    """Run the small pinned stdio fallback used by either agent host."""

    (server or mcp).run(transport="stdio", show_banner=False)


def main() -> None:
    run_stdio()


if __name__ == "__main__":
    main()


__all__ = ["create_mcp", "get_engine", "main", "mcp", "run_stdio"]
