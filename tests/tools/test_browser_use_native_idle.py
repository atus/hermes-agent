"""Native backend idle ownership through the real command adapter seam."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from tools import browser_use_cli as bu
from tools import browser_use_lifecycle as life
from tools import browser_tool_session as native


def test_owned_native_timer_yields_to_durable_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(life, "_homes", set())
    monkeypatch.setattr(life, "_transport", lambda *args: {})
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {"inactivity_timeout": 300})
    monkeypatch.setattr(native, "_prepare_session_socket_dir", lambda name: str(tmp_path))
    monkeypatch.setattr(native, "_ensure_screen_for_headed_chromium", lambda: None)
    monkeypatch.setattr(native, "_agent_browser_command_env", lambda directory: {"AGENT_BROWSER_IDLE_TIMEOUT_MS": "120000"})
    monkeypatch.setattr(native, "_apply_chromium_sandbox_args", lambda env: None)
    monkeypatch.setattr(native._cloud, "_is_headed_mode", lambda: False)
    monkeypatch.setattr(native, "_sandbox_wrap", lambda cmd, env, directory: (cmd, env))
    clock = [1000]
    monkeypatch.setattr(life.time, "time", lambda: clock[0])
    deadlines = {}

    def spawn(cmd, env, directory, command, payload):
        timeout = int(env.get("AGENT_BROWSER_IDLE_TIMEOUT_MS", "0")) / 1000
        deadlines[cmd[0]] = clock[0] + timeout if timeout else None
        tmp_path.joinpath(f"_stdout_{command}").write_text(json.dumps({"success": True, "data": {"cdpUrl": "ws://127.0.0.1:47000/owned"}}))
        tmp_path.joinpath(f"_stderr_{command}").write_text("")
        return SimpleNamespace(wait=lambda **kw: None, returncode=0)

    monkeypatch.setattr(native, "_popen_agent_browser", spawn)
    key = bu._backend_cache_key("owner", "research")
    info = {"session_name": "h_owned", "bb_session_id": None}
    with life._execution({}, "owner", "research") as record:
        assert native._spawn_and_collect(key, info, ["owned"], "get", "chrome", 10)["success"]
        life._mark_execution(record)
    clock[0] = 1150
    assert deadlines["owned"] is None or deadlines["owned"] > clock[0], "fresh retained browser lost to native 120s timer"
    with life._execution({}, "owner", "research") as record:
        life._mark_execution(record)
        clock[0] = 2000
        life._expire_owned()  # in-flight lease fences the durable janitor too
        assert list(life._state_dir().glob("*.json"))
        assert deadlines["owned"] is None or deadlines["owned"] > clock[0], "long in-flight exec lost its backend"
    assert native._spawn_and_collect("unrelated", info, ["unrelated"], "get", "chrome", 10)["success"]
    assert deadlines["unrelated"] == clock[0] + 120, "handoff must not disable native timers globally"
    stopped = []
    monkeypatch.setattr(life, "_stop_record", lambda record: stopped.append("harness"))
    monkeypatch.setattr(life, "_release_backend", lambda record: stopped.append("backend"))
    clock[0] += 301
    life._expire_owned()
    assert stopped == ["harness", "backend"]
    assert not list(life._state_dir().glob("*.json"))


def test_dead_harness_cold_spawn_keeps_idle_ownership(tmp_path, monkeypatch):
    from tools import browser_tool as bt
    from tools import browser_tool_cdp as cdp

    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(life, "_homes", set())
    monkeypatch.setattr(bt, "_active_sessions", {})
    monkeypatch.setattr(bu, "_find_cli", lambda: ["isolated-harness"])
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {"inactivity_timeout": 300})
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(bu, "_real_profile_consented", lambda: False)
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *args: None)
    monkeypatch.setattr(cdp, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(native._cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(native._cloud, "_get_browser_engine", lambda: "chrome")
    monkeypatch.setattr(native._cloud, "_is_headed_mode", lambda: False)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(native, "_browser_command_preflight", lambda: {"browser_cmd": "isolated-native"})
    monkeypatch.setattr(native, "_prepare_session_socket_dir", lambda name: str(tmp_path))
    monkeypatch.setattr(native, "_ensure_screen_for_headed_chromium", lambda: None)
    monkeypatch.setattr(native, "_agent_browser_command_env", lambda directory: {"AGENT_BROWSER_IDLE_TIMEOUT_MS": "120000"})
    monkeypatch.setattr(native, "_apply_chromium_sandbox_args", lambda env: None)
    monkeypatch.setattr(native, "_sandbox_wrap", lambda cmd, env, directory: (cmd, env))
    monkeypatch.setattr(native, "_browser_in_sandbox", lambda: False)
    clock = [1000]
    monkeypatch.setattr(life.time, "time", lambda: clock[0])
    stops = []

    def transport(record, operation):
        if operation == "stop":
            stops.append((record["pid"], record["start"]))
            return {"stopped": True}
        return {}

    monkeypatch.setattr(life, "_transport", transport)
    key = bu._backend_cache_key("saved", "research")
    with life._execution({}, "saved", "research") as record:
        record.update(pid=101, start=1, kind="chrome", endpoint="ws://127.0.0.1:47000/stale")
    path = next(life._state_dir().glob("*.json"))
    lock_inode = path.with_suffix(".lock").stat().st_ino
    seen = []
    fresh_endpoint = "ws://127.0.0.1:48000/fresh"

    def session_info(task_id):
        assert task_id == key
        info = {"session_name": "h_fresh", "bb_session_id": None, "features": {}}
        bt._active_sessions[key] = info
        return info

    monkeypatch.setattr(native, "_get_session_info", session_info)

    def spawn(cmd, env, directory, command, payload):
        owned = [life._read_record(item) for item in life._owned_paths()]
        seen.append((owned, env.get("AGENT_BROWSER_IDLE_TIMEOUT_MS")))
        assert path.with_suffix(".lock").stat().st_ino == lock_inode
        with pytest.raises(TimeoutError):
            with life._lease(path):
                pytest.fail("cold recovery released the execution lease")
        clock[0] += 1000  # a long cold launch must remain fenced from the janitor
        life._expire_owned()
        tmp_path.joinpath(f"_stdout_{command}").write_text(json.dumps(
            {"success": True, "data": {"cdpUrl": fresh_endpoint}}))
        tmp_path.joinpath(f"_stderr_{command}").write_text("")
        return SimpleNamespace(wait=lambda **kw: None, returncode=0)

    monkeypatch.setattr(native, "_popen_agent_browser", spawn)
    monkeypatch.setattr(bu, "_run_cli_killing_process_group",
                        lambda cmd, code, env, timeout: subprocess.CompletedProcess(cmd, 0, "ok", ""))
    result = json.loads(bu.browser_exec("print('ok')", session="research", task_id="saved"))
    assert result.get("success"), result
    assert stops == [(101, 1)]
    assert len(seen) == 1
    owned, timer = seen[0]
    assert len(owned) == 1, "cold backend spawned before replacement ownership was persisted"
    assert owned[0]["backend_key"] == key
    assert owned[0]["cleanup"] == "active"
    assert not {"pid", "start", "kind", "endpoint", "provider", "provider_id", "harness_stopped"} & owned[0].keys()
    assert timer is None, "cold retained backend inherited the native 120s idle timer"
    persisted = life._read_record(path)
    assert persisted["endpoint"] == fresh_endpoint
    assert persisted["backend_name"] == "h_fresh"
    assert persisted["last_execution"] == clock[0]
