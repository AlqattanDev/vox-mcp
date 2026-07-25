"""Persistent loopback-only Streamable HTTP daemon entry point."""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any

from fastmcp import FastMCP

from .config import load_user_settings
from .mcp_server import get_engine, mcp


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_MCP_PATH = "/mcp"


def _port_from_env() -> int:
    raw = os.environ.get("VOX_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("VOX_PORT must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise ValueError("VOX_PORT must be in [1, 65535]")
    return port


def run_http(
    server: FastMCP | Any | None = None,
    *,
    port: int | None = None,
) -> None:
    """Run Vox on literal IPv4 loopback; the bind host is not configurable."""

    selected_port = _port_from_env() if port is None else port
    if not 1 <= selected_port <= 65_535:
        raise ValueError("port must be in [1, 65535]")
    selected_server = server or mcp
    if server is None:
        # Create the private token before VoxStatus can issue its first menu
        # action. Engine construction initializes state only; it does not open
        # the microphone or start either speech backend.
        get_engine().ensure_control_token()
    selected_server.run(
        transport="http",
        host=DEFAULT_HOST,
        port=selected_port,
        path=DEFAULT_MCP_PATH,
        show_banner=False,
        stateless_http=False,
    )


def main() -> None:
    # Before anything reads os.environ. launchd does not hand this process the
    # shell's environment, so the settings file is the only configuration that
    # reaches the installed runtime at all.
    load_user_settings()
    _start_parent_watchdog()
    try:
        run_http()
    except KeyboardInterrupt:
        # FastMCP/uvicorn has already completed its graceful shutdown. Keep a
        # deliberate terminal stop from looking like a runtime crash.
        return


def _start_parent_watchdog(interval_seconds: float = 0.5) -> None:
    """Exit if the native status-app parent dies, avoiding an orphan daemon."""

    configured_parent = os.environ.get("VOX_PARENT_PID", "").strip()
    if not configured_parent:
        # Direct CLI/terminal use deliberately has no parent watchdog.
        return
    try:
        expected_parent = int(configured_parent)
    except ValueError:
        return
    if expected_parent <= 1:
        return

    def watch() -> None:
        while True:
            time.sleep(interval_seconds)
            if os.getppid() != expected_parent:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=watch, name="vox-parent-watchdog", daemon=True).start()


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MCP_PATH",
    "DEFAULT_PORT",
    "main",
    "run_http",
]
