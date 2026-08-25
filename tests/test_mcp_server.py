from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from httpx import ASGITransport, AsyncClient
from mcp.types import Implementation

from voxmcp.daemon import DEFAULT_HOST, DEFAULT_MCP_PATH, run_http
from voxmcp.errors import BusyError
from voxmcp.lease import LeaseManager
from voxmcp.mcp_server import (
    MAXIMUM_LISTEN_DURATION_MAX,
    MINIMUM_LISTEN_DURATION_MAX,
    create_mcp,
    run_stdio,
)
from voxmcp.models import TurnResult, TurnStatus


TOOL_NAMES = {
    "converse",
    "speak",
    "listen",
    "voice_session",
    "voice_control",
    "service",
    "diagnostics",
    "transcribe",
    "voice_registry",
    "voice_survey",
    "companion",
    "exchange_history",
}


class FakeEngine:
    METHODS = {
        "converse",
        "speak",
        "listen",
        "session",
        "control",
        "dictate",
        "read_aloud",
        "service",
        "diagnostics",
        "transcribe",
        "voice_registry",
        "survey",
        "exchange_history",
        "status",
        "health",
    }

    def __init__(self, latest_audio_path: Path) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.latest_audio_path = latest_audio_path
        self.store = SimpleNamespace(latest_stt=latest_audio_path)
        self.config = {
            "local_only": True,
            "state_dir": latest_audio_path.parent,
            "control_token": "must-not-leak",
            "nested": {"api_key": "also-secret"},
        }

    def __getattr__(self, name: str) -> Any:
        if name not in self.METHODS:
            raise AttributeError(name)

        async def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            if name == "converse":
                return TurnResult(
                    status=TurnStatus.COMPLETED,
                    session_id="voice-session",
                    transcript="the captured transcript",
                    backend="whisper-local",
                )
            if name == "status":
                return {"state": "idle", "microphone_open": False}
            if name == "health":
                return {"healthy": True, "local_only": True}
            if name == "voice_registry":
                return {"voices": [{"id": "af_sky", "provider": "kokoro"}]}
            return {"method": name, "arguments": kwargs}

        return call


@pytest.mark.asyncio
async def test_compact_tool_surface_and_host_owned_client_identity(tmp_path: Path) -> None:
    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine, control_token="test-token")

    async with Client(server) as client:
        tools = await client.list_tools()
        by_name = {tool.name: tool for tool in tools}
        assert set(by_name) == TOOL_NAMES
        assert by_name["voice_registry"].annotations.readOnlyHint is True

        spoken = await client.call_tool("speak", {"message": "Working on it."})
        conversation = await client.call_tool("converse", {"message": "Your turn."})

    speak_call = next(arguments for name, arguments in engine.calls if name == "speak")
    converse_call = next(arguments for name, arguments in engine.calls if name == "converse")
    assert speak_call["client_id"].startswith("mcp:")
    assert speak_call["client_id"] == converse_call["client_id"]
    assert "client_id" not in by_name["speak"].inputSchema["properties"]
    assert spoken.data["arguments"]["message"] == "Working on it."
    # Tool results include the user's transcript; event logging is responsible
    # for using TurnResult's redacted representation instead.
    assert conversation.data["transcript"] == "the captured transcript"


@pytest.mark.asyncio
async def test_server_shutdown_calls_engine_audio_cleanup(tmp_path: Path) -> None:
    class ShutdownEngine(FakeEngine):
        shutdown_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    engine = ShutdownEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)

    async with Client(server) as client:
        await client.list_tools()

    assert engine.shutdown_calls == 1


@pytest.mark.asyncio
async def test_host_identity_survives_new_http_sessions_and_version_updates(tmp_path: Path) -> None:
    """Host name is stable across reconnects; a second host joins shared, not busy."""

    class LeaseEngine(FakeEngine):
        def __init__(self, latest_audio_path: Path) -> None:
            super().__init__(latest_audio_path)
            self.lease = LeaseManager(ttl_seconds=600)

        async def session(self, *, client_id: str, action: str, **_kwargs: Any) -> Any:
            claim = await self.lease.claim(client_id)
            assert claim["claimed"] is True
            self.calls.append(("session", {"client_id": client_id, "action": action}))
            return {"status": "ok", "lease": claim}

        async def listen(self, *, client_id: str, **_kwargs: Any) -> Any:
            claim = await self.lease.claim(client_id)
            assert claim["claimed"] is True
            self.calls.append(("listen", {"client_id": client_id}))
            return {"status": "completed", "lease": claim}

    engine = LeaseEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)

    async with Client(
        server,
        client_info=Implementation(name="Codex", version="1.0"),
    ) as first:
        assert (await first.call_tool("voice_session", {"action": "start"})).data["status"] == "ok"

    async with Client(
        server,
        client_info=Implementation(name="Codex", version="99.0"),
    ) as reconnected:
        assert (await reconnected.call_tool("listen", {})).data["status"] == "completed"

    async with Client(
        server,
        client_info=Implementation(name="Claude Code", version="1.0"),
    ) as competitor:
        shared = await competitor.call_tool("listen", {})

    assert shared.data["status"] == "completed"
    clients = [arguments["client_id"] for _name, arguments in engine.calls]
    assert clients == ["mcp:host:codex", "mcp:host:codex", "mcp:host:claude-code"]


def _asgi_client_factory(app):
    def factory(headers=None, timeout=None, auth=None, follow_redirects=True):
        return AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 50123)),
            base_url="http://127.0.0.1",
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=follow_redirects,
        )

    return factory


@pytest.mark.asyncio
async def test_url_agent_param_separates_speakers_sharing_one_client_id(tmp_path: Path) -> None:
    """Two projects, one MCP host: the URL is what tells them apart."""

    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)
    app = server.http_app(path=DEFAULT_MCP_PATH, stateless_http=False)
    factory = _asgi_client_factory(app)

    async with app.router.lifespan_context(app):
        for label in ("infra", "mobilescape"):
            transport = StreamableHttpTransport(
                url=f"http://127.0.0.1{DEFAULT_MCP_PATH}?agent={label}",
                httpx_client_factory=factory,
            )
            async with Client(
                transport,
                client_info=Implementation(name="Claude Code", version="1.0"),
            ) as client:
                await client.call_tool("speak", {"message": f"hello from {label}"})

    speaks = [arguments for name, arguments in engine.calls if name == "speak"]
    assert [call["agent"] for call in speaks] == ["infra", "mobilescape"]
    # The lease identity is deliberately unchanged: it still follows the host.
    assert speaks[0]["client_id"] == speaks[1]["client_id"]


@pytest.mark.asyncio
async def test_url_agent_param_is_normalised(tmp_path: Path) -> None:
    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)
    app = server.http_app(path=DEFAULT_MCP_PATH, stateless_http=False)
    factory = _asgi_client_factory(app)

    async with app.router.lifespan_context(app):
        transport = StreamableHttpTransport(
            url=f"http://127.0.0.1{DEFAULT_MCP_PATH}?agent=Infra%20Team%2FProd%21",
            httpx_client_factory=factory,
        )
        async with Client(transport) as client:
            await client.call_tool("speak", {"message": "hi"})

    speak = next(arguments for name, arguments in engine.calls if name == "speak")
    assert speak["agent"] == "infra-team-prod"


@pytest.mark.asyncio
async def test_url_agent_beats_the_tool_param(tmp_path: Path) -> None:
    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)
    app = server.http_app(path=DEFAULT_MCP_PATH, stateless_http=False)
    factory = _asgi_client_factory(app)

    async with app.router.lifespan_context(app):
        transport = StreamableHttpTransport(
            url=f"http://127.0.0.1{DEFAULT_MCP_PATH}?agent=infra",
            httpx_client_factory=factory,
        )
        async with Client(transport) as client:
            await client.call_tool("speak", {"message": "hi", "agent": "spoofed"})

    speak = next(arguments for name, arguments in engine.calls if name == "speak")
    assert speak["agent"] == "infra"


@pytest.mark.asyncio
async def test_explicit_agent_param_is_used_without_a_url_param(tmp_path: Path) -> None:
    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)

    async with Client(server) as client:
        await client.call_tool("speak", {"message": "hi", "agent": "Mobilescape"})
        await client.call_tool("speak", {"message": "hi"})

    speaks = [arguments for name, arguments in engine.calls if name == "speak"]
    assert speaks[0]["agent"] == "mobilescape"
    assert speaks[1]["agent"] == "default"  # unset stays single-agent


@pytest.mark.asyncio
async def test_tool_arguments_are_adapted_to_engine_contract(tmp_path: Path) -> None:
    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)

    async with Client(server) as client:
        await client.call_tool(
            "voice_session",
            {"action": "pause", "seconds": 8, "target": "next-host", "force": True},
        )
        await client.call_tool("voice_control", {"action": "end_turn"})
        await client.call_tool("transcribe", {"audio_file": "/tmp/private.wav"})
        await client.call_tool(
            "voice_survey",
            {"turns": [{"message": "A", "voice": "af_sky"}], "agent": "companion"},
        )

    calls = {name: arguments for name, arguments in engine.calls}
    owner = calls["session"]["client_id"]
    assert owner.startswith("mcp:")
    assert calls["session"] == {
        "action": "pause",
        "client_id": owner,
        "agent": "default",
        "pause_seconds": 8,
        "target_client_id": "next-host",
        "force": True,
    }
    assert calls["control"] == {"action": "manual_end", "client_id": owner}
    assert calls["transcribe"]["path"] == "/tmp/private.wav"
    assert calls["survey"]["turns"] == [{"message": "A", "voice": "af_sky"}]
    # Without this the tool dropped `agent` on the floor and a scripted
    # interview could never be read in the companion's voice.
    assert calls["survey"]["agent"] == "companion"
    assert all(arguments["client_id"] == owner for arguments in calls.values())


@pytest.mark.asyncio
async def test_a_host_cannot_cut_the_user_off_sooner_than_the_floor(tmp_path: Path) -> None:
    """The 15-second bug: a host set the cap and truncated Ali mid-sentence.

    ``listen_duration_max`` reads to a model like a polling timeout, so models
    set it like one. The event log has caps of 15, 20, 30 and 45 s, each one a
    sentence cut off mid-word with the tail of the audio thrown away — and the
    cap shrinking turn by turn inside a single conversation. Vox's endpointer
    decides when a turn is over; this number is a backstop for a microphone
    that never closes, and no host argument may pull it below the floor.
    """

    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)

    async with Client(server) as client:
        await client.call_tool("listen", {"listen_duration_max": 15})

    arguments = next(args for name, args in engine.calls if name == "listen")
    assert arguments["listen_duration_max"] == MINIMUM_LISTEN_DURATION_MAX


@pytest.mark.asyncio
async def test_an_out_of_range_guess_is_clamped_rather_than_failed(tmp_path: Path) -> None:
    """A guess out of range cost a whole turn, and the host just guessed again.

    Raising ValueError out of the tool call produced 21 failed turns in the
    event log — twice in a row in one case — while a person sat at what they
    thought was an open microphone. The engine still never sees a value outside
    the range that can work; it is clamped on the way in instead of refused.
    """

    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)

    async with Client(server) as client:
        result = await client.call_tool(
            "listen",
            {"listen_duration_max": 301, "listen_duration_min": 400},
            raise_on_error=False,
        )

    assert result.is_error is False
    arguments = next(args for name, args in engine.calls if name == "listen")
    assert arguments["listen_duration_max"] == MAXIMUM_LISTEN_DURATION_MAX
    # A floor above the ceiling is dropped rather than honoured: obeying it
    # would hold the microphone open for the full five minutes.
    assert arguments["listen_duration_min"] == 0.0


@pytest.mark.asyncio
async def test_resources_and_prompt_are_local_and_privacy_safe(tmp_path: Path) -> None:
    latest = tmp_path / "latest.wav"
    latest.write_bytes(b"RIFF-local-recovery")
    engine = FakeEngine(latest)
    server = create_mcp(engine)

    async with Client(server) as client:
        status = await client.read_resource("voice://status")
        voices = await client.read_resource("voice://voices")
        config = await client.read_resource("voice://config")
        diagnostics = await client.read_resource("voice://diagnostics")
        audio = await client.read_resource("voice://audio/latest")
        prompt = await client.get_prompt("voice_mode")

    assert json.loads(status[0].text) == {"state": "idle", "microphone_open": False}
    assert json.loads(voices[0].text)["voices"][0]["id"] == "af_sky"
    config_value = json.loads(config[0].text)
    assert config_value["control_token"] == "<redacted>"
    assert config_value["nested"]["api_key"] == "<redacted>"
    assert json.loads(diagnostics[0].text)["method"] == "diagnostics"
    assert base64.b64decode(audio[0].blob) == latest.read_bytes()
    assert "microphone listens only" in prompt.messages[0].content.text
    assert "explicitly asks for voice mode" in prompt.messages[0].content.text
    voice_call = next(arguments for name, arguments in engine.calls if name == "voice_registry")
    diagnostic_call = next(arguments for name, arguments in engine.calls if name == "diagnostics")
    assert voice_call["client_id"].startswith("mcp:")
    assert diagnostic_call["client_id"] == voice_call["client_id"]


@pytest.mark.asyncio
async def test_http_health_and_control_are_loopback_and_token_protected(tmp_path: Path) -> None:
    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine, control_token="correct-horse")
    app = server.http_app(path="/mcp")

    transport = ASGITransport(app=app, client=("127.0.0.1", 50123))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        health = await client.get("/health")
        missing = await client.post("/control", json={"action": "cancel"})
        wrong = await client.post(
            "/control",
            json={"action": "cancel"},
            headers={"Authorization": "Bearer wrong"},
        )
        unsupported = await client.post(
            "/control",
            json={"action": "erase-everything"},
            headers={"Authorization": "Bearer correct-horse"},
        )
        accepted = await client.post(
            "/control",
            json={"action": "cancel", "client_id": "forged", "reason": "escape"},
            headers={"X-Vox-Token": "correct-horse"},
        )
        started = await client.post(
            "/control",
            json={"action": "start"},
            headers={"X-Vox-Token": "correct-horse"},
        )
        paused = await client.post(
            "/control",
            json={"action": "pause", "seconds": 12},
            headers={"X-Vox-Token": "correct-horse"},
        )
        resumed = await client.post(
            "/control",
            json={"action": "resume"},
            headers={"X-Vox-Token": "correct-horse"},
        )
        stopped = await client.post(
            "/control",
            json={"action": "stop"},
            headers={"X-Vox-Token": "correct-horse"},
        )

    assert health.status_code == 200
    assert health.json() == {"healthy": True, "local_only": True}
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert unsupported.status_code == 400
    assert accepted.status_code == 200
    assert accepted.json()["result"]["arguments"] == {
        "action": "cancel",
        "client_id": "http-control",
        "reason": "escape",
    }
    assert [started.status_code, paused.status_code, resumed.status_code, stopped.status_code] == [
        200,
        200,
        200,
        200,
    ]
    session_calls = [arguments for name, arguments in engine.calls if name == "session"]
    assert session_calls == [
        {"action": "start", "client_id": "http-control"},
        {"action": "pause", "client_id": "http-control", "pause_seconds": 12},
        {"action": "resume", "client_id": "http-control"},
        {"action": "stop", "client_id": "http-control"},
    ]

    remote_transport = ASGITransport(app=app, client=("10.0.0.20", 50123))
    async with AsyncClient(transport=remote_transport, base_url="http://10.0.0.20") as remote:
        assert (await remote.get("/health")).status_code == 403
        assert (
            (
                await remote.post(
                    "/control",
                    json={"action": "cancel"},
                    headers={"Authorization": "Bearer correct-horse"},
                )
            ).status_code
            == 403
        )


@pytest.mark.asyncio
async def test_the_turn_key_reaches_the_engine_over_loopback(tmp_path: Path) -> None:
    """The hotkey's two taps are ordinary control actions, token-authed."""

    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine, control_token="correct-horse")
    app = server.http_app(path="/mcp")

    transport = ASGITransport(app=app, client=("127.0.0.1", 50123))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        opened = await client.post(
            "/control",
            json={"action": "gate_open"},
            headers={"X-Vox-Token": "correct-horse"},
        )
        closed = await client.post(
            "/control",
            json={"action": "gate_close"},
            headers={"X-Vox-Token": "correct-horse"},
        )
        unauthorised = await client.post("/control", json={"action": "gate_open"})

    # Awaited, not the 202 fire-and-forget branch: the key needs to know
    # whether the gate actually moved.
    assert opened.status_code == 200
    assert closed.status_code == 200
    assert unauthorised.status_code == 401
    control_calls = [arguments for name, arguments in engine.calls if name == "control"]
    assert control_calls == [
        {"action": "gate_open", "client_id": "http-control"},
        {"action": "gate_close", "client_id": "http-control"},
    ]

    remote_transport = ASGITransport(app=app, client=("10.0.0.20", 50123))
    async with AsyncClient(transport=remote_transport, base_url="http://10.0.0.20") as remote:
        blocked = await remote.post(
            "/control",
            json={"action": "gate_open"},
            headers={"X-Vox-Token": "correct-horse"},
        )
    assert blocked.status_code == 403


@pytest.mark.asyncio
async def test_dictation_and_read_aloud_reach_the_engine_over_loopback(tmp_path: Path) -> None:
    """Both work with no MCP client connected — that is the point of them."""

    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine, control_token="correct-horse")
    app = server.http_app(path="/mcp")

    transport = ASGITransport(app=app, client=("127.0.0.1", 50123))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        started = await client.post(
            "/control",
            json={"action": "dictate_start"},
            headers={"X-Vox-Token": "correct-horse"},
        )
        ended = await client.post(
            "/control",
            json={"action": "dictate_end"},
            headers={"X-Vox-Token": "correct-horse"},
        )
        read = await client.post(
            "/control",
            json={"action": "read_aloud", "text": "exactly these words"},
            headers={"X-Vox-Token": "correct-horse"},
        )
        unauthorised = await client.post("/control", json={"action": "dictate_end"})

    assert [started.status_code, ended.status_code, read.status_code] == [200, 200, 200]
    assert unauthorised.status_code == 401

    dictate_calls = [arguments for name, arguments in engine.calls if name == "dictate"]
    assert dictate_calls == [
        {"action": "dictate_start", "client_id": "http-control"},
        {"action": "dictate_end", "client_id": "http-control"},
    ]
    # The selection reaches the engine unaltered; nothing on this path rewrites it.
    read_calls = [arguments for name, arguments in engine.calls if name == "read_aloud"]
    assert read_calls == [{"client_id": "http-control", "text": "exactly these words"}]


@pytest.mark.asyncio
async def test_control_route_stays_closed_without_configured_token(tmp_path: Path) -> None:
    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine, control_token="")
    app = server.http_app(path="/mcp")

    transport = ASGITransport(app=app, client=("::1", 50123))
    async with AsyncClient(transport=transport, base_url="http://[::1]") as client:
        response = await client.post(
            "/control",
            json={"action": "cancel"},
            headers={"Authorization": "Bearer anything"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == "control_token_not_configured"


@pytest.mark.asyncio
async def test_control_route_uses_engine_generated_file_token(tmp_path: Path) -> None:
    class TokenEngine(FakeEngine):
        token_reads = 0

        def ensure_control_token(self) -> str:
            self.token_reads += 1
            return "engine-file-token"

    engine = TokenEngine(tmp_path / "latest.wav")
    server = create_mcp(engine)
    app = server.http_app(path="/mcp")

    transport = ASGITransport(app=app, client=("127.0.0.1", 50123))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/control",
            json={"action": "start"},
            headers={"X-Vox-Token": "engine-file-token"},
        )

    assert response.status_code == 200
    assert engine.token_reads == 1
    assert engine.calls[-1] == (
        "session",
        {"action": "start", "client_id": "http-control"},
    )


class RunSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_daemon_and_stdio_entry_points_use_pinned_transports() -> None:
    http = RunSpy()
    run_http(http, port=9876)
    assert http.calls == [
        {
            "transport": "http",
            "host": DEFAULT_HOST,
            "port": 9876,
            "path": DEFAULT_MCP_PATH,
            "show_banner": False,
            "stateless_http": False,
        }
    ]

    stdio = RunSpy()
    run_stdio(stdio)
    assert stdio.calls == [{"transport": "stdio", "show_banner": False}]


def test_default_daemon_prepares_menu_bar_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import voxmcp.daemon as daemon

    class Runtime:
        prepared = 0

        def ensure_control_token(self) -> str:
            self.prepared += 1
            return "private-token"

    runtime = Runtime()
    http = RunSpy()
    monkeypatch.setattr(daemon, "mcp", http)
    monkeypatch.setattr(daemon, "get_engine", lambda: runtime)

    daemon.run_http(port=9876)

    assert runtime.prepared == 1
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_the_floor_key_reaches_the_engine_over_loopback(tmp_path: Path) -> None:
    """A flag flip, so it is awaited rather than backgrounded like a note."""

    engine = FakeEngine(tmp_path / "latest.wav")
    server = create_mcp(engine, control_token="correct-horse")
    app = server.http_app(path="/mcp")

    transport = ASGITransport(app=app, client=("127.0.0.1", 50123))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        toggled = await client.post(
            "/control",
            json={"action": "toggle_floor"},
            headers={"X-Vox-Token": "correct-horse"},
        )
        unauthorised = await client.post("/control", json={"action": "toggle_floor"})

    assert toggled.status_code == 200
    assert unauthorised.status_code == 401
    control_calls = [arguments for name, arguments in engine.calls if name == "control"]
    assert control_calls == [{"action": "toggle_floor", "client_id": "http-control"}]
