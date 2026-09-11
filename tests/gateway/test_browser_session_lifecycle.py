"""Browser cleanup at real gateway session boundaries, never ordinary turns."""

import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, build_session_key


@pytest.mark.asyncio
async def test_auto_reset_reaps_evicted_owner(monkeypatch, tmp_path):
    from gateway import run as gateway_run
    from hermes_cli import plugins
    from tools import browser_tool

    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="browser-test", user_id="test", chat_type="dm"
    )
    entry = SessionEntry(
        session_key=build_session_key(source), session_id="new-owner",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="dm", was_auto_reset=True,
        prev_session_id="evicted-owner", auto_reset_reason="idle",
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = []
    runner._agent_cache = {}
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda source: True
    runner._set_session_env = lambda context: None
    runner._run_agent = AsyncMock(return_value={
        "final_response": "ok", "messages": [], "tools": [],
        "history_offset": 0, "last_prompt_tokens": 0,
    })
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    calls = []
    monkeypatch.setattr(
        browser_tool, "cleanup_browser",
        lambda owner: calls.append((owner, threading.get_ident())),
    )
    plugin_calls = []
    monkeypatch.setattr(
        plugins, "invoke_hook", lambda name, **kw: plugin_calls.append(name) or []
    )
    event = MessageEvent(text="hello", source=source, message_id="1")

    assert await runner._handle_message(event) == "ok"
    assert [owner for owner, _ in calls] == ["evicted-owner"]
    assert calls[0][1] != threading.get_ident()
    assert "on_session_finalize" not in plugin_calls
    assert entry.was_auto_reset is False

    # Consumed reset metadata must not reap the old or new owner on later turns.
    assert await runner._handle_message(event) == "ok"
    assert len(calls) == 1
