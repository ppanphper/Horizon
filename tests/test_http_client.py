from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from src.orchestrator import HorizonOrchestrator


def test_fetch_all_sources_uses_http2_capable_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class RecordingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> "RecordingClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    orchestrator = object.__new__(HorizonOrchestrator)
    orchestrator.console = Console(file=StringIO())
    orchestrator.last_fetch_report = None
    orchestrator.config = SimpleNamespace(  # type: ignore[assignment]
        sources=SimpleNamespace(
            github=[],
            hackernews=SimpleNamespace(enabled=False),
            rss=[],
            reddit=SimpleNamespace(enabled=False),
            telegram=SimpleNamespace(enabled=False),
            twitter=None,
            openbb=None,
            ossinsight=SimpleNamespace(enabled=False),
            gdelt=None,
            google_news=None,
        ),
        extractors={},
    )
    monkeypatch.setattr("src.orchestrator.httpx.AsyncClient", RecordingClient)

    asyncio.run(
        orchestrator.fetch_all_sources(datetime(2026, 1, 1, tzinfo=timezone.utc))
    )

    assert captured["http2"] is True
