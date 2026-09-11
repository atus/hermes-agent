"""Hard browser ownership is independent of DB-row ownership and task IDs."""

from unittest.mock import Mock

import pytest

from run_agent import AIAgent


@pytest.fixture
def browser_agent(monkeypatch):
    agent = object.__new__(AIAgent)
    agent.session_id = "logical-owner"
    agent._current_task_id = "different-turn-task"
    cleanup = Mock()
    monkeypatch.setattr("run_agent.cleanup_browser", cleanup)
    monkeypatch.setattr("run_agent.cleanup_vm", lambda *a: None)
    monkeypatch.setattr("tools.process_registry.process_registry.kill_all", lambda **kw: None)
    monkeypatch.setattr("tools.computer_use.release_computer_use_session", lambda *a: None)
    return agent, cleanup


@pytest.mark.parametrize("end_row", [True, False])
def test_hard_close_reaps_owner(browser_agent, end_row):
    agent, cleanup = browser_agent
    agent._end_session_on_close = end_row

    agent.close()

    cleanup.assert_called_once_with("logical-owner")


def test_soft_release_preserves_browser(browser_agent):
    agent, cleanup = browser_agent

    agent.release_clients()

    cleanup.assert_not_called()


def test_borrowed_owner_is_not_reaped(browser_agent):
    agent, cleanup = browser_agent
    agent._owns_browser_session = False

    agent.close()

    cleanup.assert_not_called()
