"""Gateway restart and conversation boundaries preserve scoped harness owners."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tools import browser_use_lifecycle as life
from gateway import run as gateway_run
from gateway.run_startup import GatewayStartupMixin


@pytest.mark.asyncio
async def test_boot_recovers_idle_harness_without_new_browser_call(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from tools import browser_tool_lifecycle as worker
    from tools import browser_use_cli as bu

    monkeypatch.setattr(worker, "_start_browser_cleanup_thread", lambda: None)
    with life._execution({}, "saved-session", "research"):
        pass
    secondary = tmp_path / "secondary"
    secondary.mkdir()
    with worker._session_owner_scope("", owner_home=str(secondary)):
        with life._execution({}, "saved-secondary", "secondary"):
            pass
    expected_homes = {str(get_hermes_home().resolve()), str(secondary)}
    monkeypatch.setattr(gateway_run, "_multiplex_profile_homes", lambda config: [("secondary", secondary)])
    monkeypatch.setattr(life, "_homes", set())
    starts = []
    monkeypatch.setattr(worker, "_start_browser_cleanup_thread", lambda: starts.append(set(life._homes)))
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: None)
    monkeypatch.setattr("tools.process_registry.process_registry.recover_from_checkpoint", lambda: 0)
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        hooks=SimpleNamespace(discover_and_load=lambda: None),
        _start_register_plugins_relay_hooks=lambda: None,
        _subscribe_plugin_rewire=lambda manager: None,
        _recover_secondary_process_checkpoints=lambda registry: 0,
        _recover_unclean_sessions=AsyncMock(return_value=(0, 0)),
        _consume_clean_shutdown_marker=AsyncMock(return_value=0),
        _suspend_stuck_loop_sessions=lambda: 0,
    )
    await GatewayStartupMixin._start_recover_previous_run(runner)
    assert starts, "gateway boot must restart the existing lifecycle worker for durable ownership"
    assert all(homes == expected_homes for homes in starts), "bind every served owner before starting the orphan pass"
    monkeypatch.setattr(life.time, "time", lambda: 10**12)
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {"inactivity_timeout": 300})
    stops = []
    monkeypatch.setattr(life, "_stop_record", lambda record: stops.append(record["name"]))
    worker._cleanup_inactive_browser_sessions()
    assert sorted(stops) == ["research", "secondary"]


@pytest.mark.asyncio
async def test_new_detaches_owner_and_resume_reuses_runtime(monkeypatch):
    from gateway.slash_commands_session import GatewaySessionCommandsMixin
    from tools import browser_tool_lifecycle as worker

    monkeypatch.setattr(worker, "_start_browser_cleanup_thread", lambda: None)
    env = {}
    with life._execution(env, "old-session", "research"):
        pass
    path = next(life._state_dir().glob("*.json"))
    before = life._read_record(path)
    old_agent = SimpleNamespace(session_id="old-session")
    closed = Mock()
    soft = Mock()

    async def execute(fn, *args):
        return fn(*args)

    runner = SimpleNamespace(
        _cached_agent_for=lambda key: old_agent,
        _cleanup_agent_resources=closed,
        _release_evicted_agent_soft=soft,
        _run_release_in_profile_scope=lambda fn, args, key: fn(*args),
        _run_housekeeping_in_executor=execute,
    )
    await GatewaySessionCommandsMixin._cleanup_old_agent_for_reset(runner, "agent:main:room")
    closed.assert_not_called()
    soft.assert_called_once_with(old_agent)
    detached = life._read_record(path)
    assert detached["cleanup"] == "detached"
    assert detached["last_execution"] == before["last_execution"]
    resumed = {}
    with life._execution(resumed, "old-session", "research"):
        pass
    assert resumed["BH_RUNTIME_DIR"] == env["BH_RUNTIME_DIR"]
    assert life._read_record(path)["cleanup"] == "active"
