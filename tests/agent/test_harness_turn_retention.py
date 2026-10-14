"""Turn completion must not replace the harness owner's idle retention boundary."""
from types import SimpleNamespace
from unittest.mock import Mock

import run_agent
from agent import chat_completion_helpers as helpers
from tools import browser_use_lifecycle as life


def test_turn_completion_retains_owned_harness(monkeypatch):
    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: None)
    with life._execution({}, "saved", "default") as record:
        record["backend_key"] = "saved"
    closed = Mock()
    monkeypatch.setattr(run_agent, "cleanup_browser", closed)
    monkeypatch.setattr(run_agent, "cleanup_vm", lambda task: None)
    monkeypatch.setattr("tools.browser_tool_cloud._is_headed_mode", lambda: False)
    helpers.cleanup_task_resources(SimpleNamespace(verbose_logging=False), "saved")
    closed.assert_not_called()
