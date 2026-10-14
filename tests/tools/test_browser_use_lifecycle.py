"""Durable harness ownership contracts; no real browser or existing endpoint."""
import json
import subprocess

import pytest

from tools import browser_use_cli as bu
from tools import browser_use_lifecycle as life


@pytest.fixture(autouse=True)
def no_worker(monkeypatch):
    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(life, "_homes", set())


def test_profile_context_controls_runtime_and_expiry(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from agent.secret_scope import get_secret
    from tools.browser_tool_lifecycle import _session_owner_scope

    monkeypatch.setattr(life.time, "time", lambda: 1000)
    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    runtimes = []
    for home, timeout in zip(homes, (30, 300)):
        home.mkdir()
        home.joinpath("config.yaml").write_text(f"browser:\n  inactivity_timeout: {timeout}\n")
        home.joinpath(".env").write_text(f"BROWSERBASE_API_KEY={home.name}\n")
        with _session_owner_scope("", owner_home=str(home)):
            env = {}
            with life._execution(env, "same-owner", "same-name"):
                pass
            runtimes.append(env["BH_RUNTIME_DIR"])
            assert env.get("BH_CONFIG_DIR", "").startswith(str(home)), "harness auth/config must use the served profile"
    assert runtimes[0] != runtimes[1]
    stopped = []
    monkeypatch.setattr(life, "_stop_record", lambda record: stopped.append(
        (str(get_hermes_home()), get_secret("BROWSERBASE_API_KEY"))))
    monkeypatch.setattr(life.time, "time", lambda: 1100)
    life._expire_owned()
    assert stopped == [(str(homes[0]), "profile-a")]


def test_explicit_stop_retains_failed_owned_records(monkeypatch):
    with life._execution({}, "owner", "retry"):
        pass
    path = next(life._state_dir().glob("*.json"))
    monkeypatch.setattr(bu, "_find_cli", lambda: ["must-not-use-global-reload"])
    monkeypatch.setattr(bu, "_run_cli_killing_process_group", lambda *args: None)

    def fail(record):
        raise RuntimeError("cloud stop failed")

    monkeypatch.setattr(life, "_stop_record", fail)
    with pytest.raises(RuntimeError, match="cloud stop failed"):
        bu.stop_harness_daemons()
    assert path.exists()
    assert life._read_record(path)["cleanup"] == "retry"
    stopped = []
    monkeypatch.setattr(life, "_stop_record", lambda record: stopped.append(record["name"]))
    bu.stop_harness_daemons()
    assert stopped == ["retry"]
    assert not path.exists()


def test_provider_release_waits_for_harness_stop(monkeypatch):
    from types import SimpleNamespace
    from tools import browser_tool as bt
    from tools import browser_tool_lifecycle as worker
    from tools import browser_tool_cloud as cloud

    key = bu._backend_cache_key("owner", "provider")
    info = {"bb_session_id": "owned-cloud-id", "session_name": ""}
    with life._execution({}, "owner", "provider") as record:
        record.update(backend_key=key, provider="probe", provider_id="owned-cloud-id")
    path = next(life._state_dir().glob("*.json"))
    closed = []
    monkeypatch.setattr(cloud, "_get_cloud_provider", lambda: SimpleNamespace(
        name="probe", close_session=lambda session_id: closed.append(session_id)))
    monkeypatch.setattr(bt, "_active_sessions", {key: info})

    def fail(record):
        raise RuntimeError("harness stop refused")

    monkeypatch.setattr(life, "_stop_record", fail)
    with pytest.raises(RuntimeError, match="harness stop refused"):
        worker._release_session_resources(key, info)
    assert not closed
    assert key in bt._active_sessions
    assert path.exists()
    stopped = []
    monkeypatch.setattr(life, "_stop_record", lambda record: stopped.append(record["name"]))
    worker._release_session_resources(key, info)
    assert stopped == ["provider"]
    assert "owned-cloud-id" in closed
    assert not path.exists()


def test_restart_protects_owned_backend_from_legacy_orphan_reap(tmp_path, monkeypatch):
    from tools import browser_tool as bt
    from tools import browser_tool_lifecycle as worker

    with life._execution({}, "saved", "research") as record:
        record["backend_name"] = "h_owned"
    tmp_path.joinpath("agent-browser-h_owned").mkdir()
    tmp_path.joinpath("agent-browser-h_unrelated").mkdir()
    monkeypatch.setattr(bt, "_socket_safe_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(bt, "_active_sessions", {})
    reaped = []
    monkeypatch.setattr(worker, "_reap_socket_dir", lambda directory, name, tracked: reaped.append(name) or False)
    worker._reap_orphaned_browser_sessions()
    assert reaped == ["h_unrelated"]


def test_unowned_runtime_is_never_adopted():
    from hermes_constants import get_hermes_home
    key = life._record_id(get_hermes_home().resolve(), "owner", "unowned")
    runtime = life._runtime_dir(key)
    runtime.joinpath("bu.pid").write_text("12345")
    with pytest.raises(RuntimeError, match="unowned"):
        with life._execution({}, "owner", "unowned"):
            pytest.fail("An unowned endpoint must not receive execution")
    assert not list(life._state_dir().glob("*.json"))


def test_expired_resume_recreates_cold_before_renewing(monkeypatch):
    monkeypatch.setattr(life.time, "time", lambda: 1000)
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {"inactivity_timeout": 300})
    with life._execution({}, "saved", "research"):
        pass
    stopped = []
    monkeypatch.setattr(life, "_stop_record", lambda record: stopped.append(record["last_execution"]))
    monkeypatch.setattr(life.time, "time", lambda: 2000)
    with life._execution({}, "saved", "research") as record:
        assert stopped == [1000], "a missed worker tick must not revive an expired daemon"
        assert not record.get("harness_stopped")


@pytest.mark.parametrize("backend", ["cloud", "local", "external"])
def test_expired_backend_recreates_fresh_cache(monkeypatch, backend):
    from types import SimpleNamespace
    from tools import browser_tool as bt
    from tools import browser_tool_session as sessions
    from tools import browser_tool_cloud as cloud
    from tools import browser_tool_lifecycle as worker

    key = bu._backend_cache_key("saved", "cold")
    old = {"session_name": "h_owned" if backend == "local" else "",
           "bb_session_id": "cloud-old" if backend == "cloud" else None,
           "cdp_url": "ws://127.0.0.1:47000/owned", "features": {}}
    monkeypatch.setattr(bt, "_active_sessions", {key: old})
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_maybe_stop_recording", lambda key: None)
    monkeypatch.setattr(sessions, "_local_backend_process_dead", lambda info: False)
    monkeypatch.setattr(worker, "_session_has_expired", lambda info: False)
    monkeypatch.setattr(bt, "_browser_session_backend", lambda key: SimpleNamespace(ensure_healthy=lambda: True))
    monkeypatch.setattr(sessions, "_create_session_for_key", lambda *args: {**old, "bb_session_id": "cloud-new"})
    monkeypatch.setattr(sessions._cdp, "_ensure_cdp_supervisor", lambda key: None)
    monkeypatch.setattr(worker._cdp, "_stop_cdp_supervisor", lambda key: None)
    monkeypatch.setattr(worker, "_stop_record", lambda record: None, raising=False)
    events = []
    monkeypatch.setattr(life, "_stop_record", lambda record: events.append("harness"))
    monkeypatch.setattr(sessions, "_run_browser_command", lambda key, command, args, **kw: events.append(command))
    monkeypatch.setattr(cloud, "_get_cloud_provider", lambda: SimpleNamespace(
        name="probe", close_session=lambda sid: events.append(sid)))
    monkeypatch.setattr(life.time, "time", lambda: 1000)
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {"inactivity_timeout": 300})
    with life._execution({}, "saved", "cold") as record:
        record.update(backend_key=key, backend_name=old["session_name"], endpoint=old["cdp_url"])
        if backend == "cloud":
            record.update(provider="probe", provider_id="cloud-old")
    monkeypatch.setattr(life.time, "time", lambda: 2000)
    with life._execution({}, "saved", "cold"):
        fresh = sessions._get_session_info(key)
        assert fresh is not old, "resume must not return a closed cached backend"
        assert bt._active_sessions[key] is fresh
        assert events[0] == "harness"
        if backend == "cloud":
            assert events.count("cloud-old") == 1
        if backend == "local":
            assert "close" in events, "owned local browser must stop before cold recreation"
        if backend == "external":
            assert "close" not in events, "external Chrome must never be closed"


def test_missing_cloud_authority_retains_ownership():
    import sys
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    with life._execution({}, "owner", "cloud") as record:
        pass
    record.update(pid=dead.pid, start=1, kind="cloud")
    with pytest.raises(RuntimeError, match="cloud.*unavailable"):
        life._stop_record(record)


def test_backend_stop_does_not_lock_unrelated_execution(monkeypatch):
    with life._execution({}, "one", "idle") as record:
        record["backend_key"] = "backend-one"
    monkeypatch.setattr(life, "_stop_record", lambda record: None)
    with life._execution({}, "two", "busy") as record:
        record["backend_key"] = "backend-two"
        life._stop_owned("backend-one")
    assert len(list(life._state_dir().glob("*.json"))) == 1


def test_restart_reuses_owned_backend_without_allocating_again(monkeypatch):
    calls = []
    monkeypatch.setattr(bu, "_find_cli", lambda: ["isolated-probe"])
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *args: None)
    monkeypatch.setattr(bu, "is_legacy_browser_use_cloud_config", lambda cfg: False)
    monkeypatch.setattr(life, "_transport", lambda record, operation: {"pid": 123, "start": 1, "healthy": True})
    monkeypatch.setattr(bu, "_run_cli_killing_process_group",
                        lambda cmd, code, env, timeout: subprocess.CompletedProcess(cmd, 0, "ok", ""))

    def allocate(env, *args):
        calls.append(True)
        env["BU_CDP_WS"] = "ws://127.0.0.1:47000/owned-browser"

    monkeypatch.setattr(bu, "_route_backend", allocate)
    assert json.loads(bu.browser_exec("print(1)", session="research", task_id="saved"))["success"]
    monkeypatch.setattr(life, "_homes", set())
    assert json.loads(bu.browser_exec("print(1)", session="research", task_id="saved"))["success"]
    assert len(calls) == 1, "restart must not allocate a second backend behind a retained live harness"
    stopped = []

    def dead_transport(record, operation):
        if operation == "stop":
            stopped.append(record["name"])
            return {"stopped": True}
        return {}

    monkeypatch.setattr(life, "_transport", dead_transport)
    result = json.loads(bu.browser_exec("print(1)", session="research", task_id="saved"))
    assert result.get("success"), result
    assert len(calls) == 2
    assert stopped == ["research"]


def test_owned_lightpanda_survives_restart_orphan_pass(tmp_path, monkeypatch):
    from tools import browser_lightpanda as lp
    with life._execution({}, "saved", "research") as record:
        record["backend_name"] = "lp_owned"
    tmp_path.joinpath("lp_owned.json").write_text(json.dumps({"pid": 123, "port": 48000, "owner_pid": 0}))
    monkeypatch.setattr(lp, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(lp, "_is_lightpanda_process", lambda *args: True)
    killed = []
    monkeypatch.setattr(lp, "_tree_kill", lambda *args: killed.append(args))
    lp.reap_orphaned_lightpanda()
    assert killed == []
    assert tmp_path.joinpath("lp_owned.json").exists()


def test_routing_failure_does_not_renew_execution_time(monkeypatch):
    monkeypatch.setattr(bu, "_find_cli", lambda: ["isolated-probe"])
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *args: None)
    monkeypatch.setattr(bu, "is_legacy_browser_use_cloud_config", lambda cfg: False)
    monkeypatch.setattr(bu, "_route_backend", lambda *args: None)
    monkeypatch.setattr(bu, "_run_cli_killing_process_group",
                        lambda cmd, code, env, timeout: subprocess.CompletedProcess(cmd, 0, "ok", ""))
    monkeypatch.setattr(life.time, "time", lambda: 1000)
    assert json.loads(bu.browser_exec("print(1)", session="research", task_id="saved"))["success"]
    monkeypatch.setattr(life.time, "time", lambda: 1100)
    monkeypatch.setattr(bu, "_route_backend", lambda *args: "provider unavailable")
    assert json.loads(bu.browser_exec("print(1)", session="research", task_id="saved")).get("error")
    path = next(life._state_dir().glob("*.json"))
    assert life._read_record(path)["last_execution"] == 1000


@pytest.mark.parametrize("changed", [
    {"pid": True, "start": 1}, {"pid": 123, "start": float("nan")},
    {"endpoint": "/outside/socket"}, {"cleanup": "impostor"},
])
def test_untrusted_record_identity_is_rejected(changed):
    with life._execution({}, "owner", "record"):
        pass
    path = next(life._state_dir().glob("*.json"))
    record = life._read_record(path)
    record.update(changed)
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        life._read_record(path)


@pytest.mark.parametrize("replacement", [{"pid": 202, "start": 2}, {"pid": 101, "start": 2}])
def test_rejected_generation_never_becomes_authority(monkeypatch, replacement):
    original = {"pid": 101, "start": 1}
    monkeypatch.setattr(life, "_transport", lambda record, operation: dict(original))
    with life._execution({}, "owner", "generation") as record:
        record["endpoint"] = "ws://127.0.0.1:47000/owned"
    path = next(life._state_dir().glob("*.json"))

    def changed(record, operation):
        if operation == "stop":
            assert {key: record[key] for key in original} == original
            raise RuntimeError("generation changed")
        return dict(replacement)

    monkeypatch.setattr(life, "_transport", changed)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="generation"):
            with life._execution({}, "owner", "generation") as record:
                life._reuse_endpoint(record, {})
        persisted = life._read_record(path)
        assert {key: persisted[key] for key in original} == original
        assert persisted["cleanup"] == "retry"
    with pytest.raises(RuntimeError, match="generation"):
        life._stop_owned()
    assert {key: life._read_record(path)[key] for key in original} == original


def test_backend_names_are_owner_scoped():
    first = bu._backend_cache_key("owner-one", "research")
    assert first == bu._backend_cache_key("owner-one", "research")
    assert first != bu._backend_cache_key("owner-two", "research")


def test_exec_persists_scoped_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(bu, "_find_cli", lambda: ["isolated-probe"])
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(bu, "_route_backend", lambda *args: None)
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda *args: None)
    monkeypatch.setattr(bu, "is_legacy_browser_use_cloud_config", lambda cfg: False)
    seen = []

    def run(cmd, code, env, timeout):
        seen.append(dict(env))
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(bu, "_run_cli_killing_process_group", run)
    assert json.loads(bu.browser_exec("print('ok')", session="research", task_id="one"))["success"]
    assert json.loads(bu.browser_exec("print('ok')", session="research", task_id="two"))["success"]
    assert seen[0].get("BH_RUNTIME_DIR"), "execution must not use the global harness runtime"
    assert seen[0]["BH_RUNTIME_DIR"] != seen[1]["BH_RUNTIME_DIR"]
    assert seen[0]["BU_NAME"] == seen[1]["BU_NAME"] == "research"
    from hermes_constants import get_hermes_home
    records = list((get_hermes_home() / "cache/browser-use/owned").glob("*.json"))
    assert len(records) == 2
    assert {json.loads(p.read_text())["owner"] for p in records} == {"one", "two"}


def test_worker_expires_only_idle_owned_records(monkeypatch):
    from tools import browser_tool_lifecycle as worker
    from hermes_constants import get_hermes_home

    monkeypatch.setattr(life.time, "time", lambda: 1000)
    with life._execution({}, "owner", "expired"):
        pass
    monkeypatch.setattr(life.time, "time", lambda: 2000)
    with life._execution({}, "owner", "fresh"):
        pass
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {"inactivity_timeout": 300})
    stopped = []
    monkeypatch.setattr(life, "_stop_record", lambda record: stopped.append(record["name"]), raising=False)
    # Simulate losing process-local bookkeeping, retaining only the profile registry.
    monkeypatch.setattr(life, "_homes", {str(get_hermes_home().resolve())}, raising=False)
    with life._execution({}, "owner", "inflight"):
        monkeypatch.setattr(life.time, "time", lambda: 2300)
        with life._execution({}, "owner", "recent"):
            pass
        monkeypatch.setattr(life.time, "time", lambda: 2400)
        worker._cleanup_inactive_browser_sessions()
        assert sorted(stopped) == ["expired", "fresh"]
    records = list(life._state_dir().glob("*.json"))
    assert sorted(json.loads(p.read_text())["name"] for p in records) == ["inflight", "recent"]


@pytest.fixture
def fake_harness(tmp_path):
    """A real isolated IPC peer using the installed harness protocol, never Chrome."""
    import browser_harness
    package = tmp_path / "peer" / "browser_harness"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text(
        f"__path__.append({str(next(iter(browser_harness.__path__)))!r})\n")
    package.joinpath("daemon.py").write_text('''
import asyncio, json, os
from browser_harness import _ipc as ipc
name = os.environ["BU_NAME"]
async def handle(reader, writer):
    req = json.loads(await reader.readline())
    if req.get("meta") == "shutdown":
        failed = os.path.exists(os.environ["FAIL_STOP"])
        response = {"error": "cloud stop failed"} if failed else {"ok": True}
    else:
        failed = False
        response = {"pong": True, "pid": os.getpid(), "browser_kind": "cloud"}
    writer.write((json.dumps(response) + "\\n").encode())
    await writer.drain()
    writer.close()
    if req.get("meta") == "shutdown" and not failed:
        asyncio.get_running_loop().call_later(.05, os._exit, 0)
ipc.pid_path(name).write_text(str(os.getpid()))
asyncio.run(ipc.serve(name, handle))
''')
    return package.parent


def test_real_ipc_shutdown_retries_cloud_failure(fake_harness, tmp_path, monkeypatch):
    import os
    import sys
    import time

    failure = tmp_path / "fail-stop"
    failure.touch()
    env = {**os.environ, "PYTHONPATH": str(fake_harness), "FAIL_STOP": str(failure)}
    peer = None
    try:
        with life._execution(env, "owner", "cloud"):
            peer = subprocess.Popen([sys.executable, "-m", "browser_harness.daemon"], env=env)
            pidfile = life.Path(env["BH_RUNTIME_DIR"]) / "bu.pid"
            deadline = time.monotonic() + 10
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert pidfile.exists()
            time.sleep(.1)
        path = next(life._state_dir().glob("*.json"))
        owned = json.loads(path.read_text())
        assert owned.get("pid") == peer.pid, "persist the verified daemon generation after execution"
        stale = {**owned, "start": owned["start"] - 1}
        with pytest.raises(RuntimeError, match="generation changed"):
            life._stop_record(stale)
        assert peer.poll() is None
        with pytest.raises(RuntimeError, match="cloud stop failed"):
            life._stop_record(owned)
        assert peer.poll() is None
        failure.unlink()
        # A gateway can die after spawning but before publishing the generation.
        owned.pop("pid")
        owned.pop("start")
        life._write_record(path, owned)
        life._stop_owned()
        assert peer.wait(timeout=5) == 0
        assert not path.exists()
    finally:
        if peer is not None and peer.poll() is None:
            peer.kill()
            peer.wait(timeout=5)
