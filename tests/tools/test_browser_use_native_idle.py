"""Native backend idle ownership through the real command adapter seam."""
import json
from types import SimpleNamespace

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
