"""Suite-wide safety net for the one thing a test can break outside itself.

``copy_to_clipboard`` shells out to ``pbcopy``, and the clipboard is not test
state — it is whatever the person running the suite had copied. Every test that
drove a whole listen turn without injecting a runner put the fixture transcript
on it, so running the suite pasted a literal "hello world" into their next ⌘V.
"""

from __future__ import annotations

from typing import Any, Iterator, Sequence

import pytest

from voxmcp import dictation


@pytest.fixture(autouse=True)
def never_touch_the_real_clipboard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts: list[bytes] = []

    def record(_argv: Sequence[str], payload: bytes, **_kwargs: Any) -> None:
        attempts.append(payload)

    monkeypatch.setattr(dictation, "_pbcopy", record)
    yield
    assert not attempts, (
        f"this test wrote {attempts!r} to the real clipboard — "
        "inject a runner (engine._clipboard_runner) instead"
    )
