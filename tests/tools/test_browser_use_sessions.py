"""Session ownership regressions; never connect to a real browser."""
import json
import sys

import pytest


@pytest.mark.parametrize("alias", ["research\n", "bad name", "x" * 65, 123])
def test_raw_alias_validation(alias, monkeypatch):
    monkeypatch.setattr(cli, "_find_cli", lambda: None)
    result = json.loads(cli.browser_exec("print(1)", session=alias, task_id="owner"))
    assert "Invalid session name" in result["error"]


def test_provider_key_is_owner_scoped(monkeypatch):
    from tools import browser_tool as browser
    seen = []
    monkeypatch.setattr(browser, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(browser, "_get_cloud_provider", lambda: object())
    monkeypatch.setattr(browser, "_get_session_info", lambda key: seen.append(key) or {"cdp_url": "ws://fake"})
    for owner in ("a", "b", "a"):
        assert cli._resolve_backend_cdp({}, owner, "research") is None
    assert seen[0] != seen[1]
    assert seen[0] == seen[2]

from tools import browser_use_cli as cli


def test_cleanup_owner_exact_targets(tmp_path, monkeypatch):
    from tools import browser_use_sessions as sessions
    from tools import browser_tool
    record = sessions.register_lane('owner', 'research', {})
    other = sessions.register_lane('other', 'research', {})
    tabs = record.with_suffix('.tabs.json')
    tabs.write_text(json.dumps({'epoch': 'epoch', 'targets': ['a', 'gone'], 'current': 'a'}))
    calls = []

    def request(runtime, payload):
        calls.append(payload)
        if payload.get('method') == 'Target.getTargetInfo':
            return {'result': {'targetInfo': {'targetId': 'epoch'}}}
        if payload.get('method') == 'Target.getTargets':
            return {'result': {'targetInfos': [{'targetId': 'foreign'}] + ([{'targetId': 'a'}] if not any(c.get('method') == 'Target.closeTarget' for c in calls) else [])}}
        return {'ok': True, 'result': {'success': True}}

    monkeypatch.setattr(sessions, '_daemon_request', request, raising=False)
    monkeypatch.setattr(browser_tool, '_cleanup_single_browser_session', lambda key: None)
    browser_tool.cleanup_browser('owner')
    assert [call['params']['targetId'] for call in calls if call.get('method') == 'Target.closeTarget'] == ['a']
    assert calls[-1] == {'meta': 'shutdown'}
    assert not record.exists() and not tabs.exists()
    assert other.exists()
    browser_tool.cleanup_browser('owner')
    assert len([call for call in calls if call.get('meta') == 'shutdown']) == 1


def test_bu_only_emergency_cleanup(monkeypatch):
    from tools import browser_use_sessions as sessions
    from tools import browser_tool as browser
    record = sessions.register_lane('only-bu', '', {})
    calls = []
    monkeypatch.setattr(sessions, '_daemon_request', lambda runtime, payload: calls.append(payload) or {'ok': True})
    monkeypatch.setattr(browser, '_active_sessions', {})
    monkeypatch.setattr(browser, '_cleanup_done', False)
    monkeypatch.setattr(browser, '_reap_orphaned_browser_sessions', lambda: None)
    monkeypatch.setattr(browser, '_cleanup_single_browser_session', calls.append)
    browser._emergency_cleanup_all_sessions()
    assert not record.exists()
    assert sessions.lane_name('only-bu') in calls


def test_headless_turn_preserves_bu(monkeypatch):
    from types import SimpleNamespace
    from agent import chat_completion_helpers as chat
    from tools import browser_tool as browser
    calls = []
    monkeypatch.setattr(chat, 'is_persistent_env', lambda task: False)
    monkeypatch.setattr(chat, '_ra', lambda: SimpleNamespace(cleanup_vm=lambda task: None, cleanup_browser=calls.append))
    monkeypatch.setattr(browser, '_is_headed_mode', lambda: False)
    monkeypatch.setattr(cli, 'is_browser_use_cli_mode', lambda: True)
    chat.cleanup_task_resources(SimpleNamespace(verbose_logging=False, session_id='owner'), 'turn')
    assert calls == []


def test_workspace_follows_session(tmp_path, monkeypatch):
    script = tmp_path / 'fake_cli.py'
    script.write_text('import sys; sys.stdin.read()')
    monkeypatch.delenv('BH_AGENT_WORKSPACE', raising=False)
    monkeypatch.setattr(cli, '_find_cli', lambda: [sys.executable, str(script)])
    monkeypatch.setattr(cli, '_resolve_backend_cdp', lambda *a, **k: None)
    results = [json.loads(cli.browser_exec('print(1)', session_id='owner', task_id=turn)) for turn in ('one', 'two')]
    assert results[0]['workspace'] == results[1]['workspace']


def test_cleanup_unlisted_popup(monkeypatch):
    from tools import browser_use_sessions as sessions
    record = sessions.register_lane('owner', '', {})
    record.with_suffix('.tabs.json').write_text(json.dumps({'epoch': 'epoch', 'targets': ['a']}))
    live = {'a': {'targetId': 'a'}, 'child': {'targetId': 'child', 'type': 'page', 'openerId': 'a'},
            'foreign': {'targetId': 'foreign', 'type': 'page', 'openerId': 'user'}}

    def request(runtime, payload):
        method = payload.get('method')
        if method == 'Target.getTargetInfo':
            return {'result': {'targetInfo': {'targetId': 'epoch'}}}
        if method == 'Target.getTargets':
            return {'result': {'targetInfos': list(live.values())}}
        if method == 'Target.closeTarget':
            live.pop(payload['params']['targetId'])
            return {'result': {'success': True}}
        return {'ok': True}

    monkeypatch.setattr(sessions, '_daemon_request', request)
    sessions.cleanup_owner('owner')
    assert set(live) == {'foreign'}
    assert not record.exists()


@pytest.mark.parametrize('disappear_after', [0.04, None])
def test_cleanup_waits_for_close(monkeypatch, disappear_after):
    from types import SimpleNamespace
    from tools import browser_use_sessions as sessions
    from tools import browser_tool

    record = sessions.register_lane('delayed-owner', '', {})
    journal = record.with_suffix('.tabs.json')
    journal.write_text(json.dumps({'epoch': 'epoch', 'targets': ['owned']}))
    live = {'owned': {'targetId': 'owned'}, 'foreign': {'targetId': 'foreign'}}
    clock = {'now': 0.0}
    calls = []

    def sleep(seconds):
        clock['now'] += seconds

    def request(runtime, payload):
        method = payload.get('method')
        calls.append(payload)
        if method == 'Target.getTargetInfo':
            return {'result': {'targetInfo': {'targetId': 'epoch'}}}
        if method == 'Target.closeTarget':
            assert payload['params']['targetId'] == 'owned'
            return {'result': {'success': True}}
        if method == 'Target.getTargets':
            if disappear_after is not None and clock['now'] >= disappear_after:
                live.pop('owned', None)
            return {'result': {'targetInfos': list(live.values())}}
        assert payload == {'meta': 'shutdown'}
        assert 'owned' not in live
        return {'ok': True}

    monkeypatch.setattr(sessions, 'time', SimpleNamespace(monotonic=lambda: clock['now'], sleep=sleep))
    monkeypatch.setattr(sessions, '_daemon_request', request)
    monkeypatch.setattr(browser_tool, '_cleanup_single_browser_session', lambda name: None)
    sessions.cleanup_owner('delayed-owner')

    assert 'foreign' in live
    if disappear_after is None:
        assert record.exists() and journal.exists()
        assert not any(call.get('meta') == 'shutdown' for call in calls)
        assert sessions._IPC_TIMEOUT <= clock['now'] <= sessions._IPC_TIMEOUT + sessions._LOCK_POLL_SECONDS
        return

    assert set(live) == {'foreign'}
    assert not record.exists() and not journal.exists()
    assert calls[-1] == {'meta': 'shutdown'}
    assert disappear_after <= clock['now'] < sessions._IPC_TIMEOUT


def test_same_lane_calls_serialized(monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    entered = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr(cli, '_find_cli', lambda: ['fake'])
    monkeypatch.setattr(cli, '_resolve_backend_cdp', lambda *a, **k: None)

    def execute(*args, **kwargs):
        calls.append(kwargs['env']['BU_NAME'])
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(stdout='', stderr='', returncode=0)

    monkeypatch.setattr(cli, '_run_cli', execute)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cli.browser_exec, 'print(1)', task_id='owner')
        assert entered.wait(2)
        second = pool.submit(cli.browser_exec, 'print(2)', task_id='owner')
        try:
            time.sleep(0.1)
            assert len(calls) == 1
        finally:
            release.set()
        assert json.loads(first.result())['success']
        assert json.loads(second.result())['success']


def test_cleanup_drains_lane(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tools import browser_use_sessions as sessions
    record = sessions.register_lane('owner', '', {})
    reached = threading.Event()
    monkeypatch.setattr(sessions, '_daemon_request', lambda *args: reached.set() or {'ok': True})
    with ThreadPoolExecutor(1) as pool:
        with sessions.lane_call('owner', ''):
            future = pool.submit(sessions.cleanup_owner, 'owner')
            assert not reached.wait(0.1), 'Cleanup raced an executing lane'
        future.result(timeout=3)
    assert reached.is_set()
    assert not record.exists()


def test_cleanup_cancels_executing_cli(tmp_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from tools import browser_use_sessions as sessions
    started = tmp_path / 'started'
    script = tmp_path / 'blocking.py'
    script.write_text(f"import time; from pathlib import Path; Path({str(started)!r}).touch(); time.sleep(60)")
    monkeypatch.setattr(cli, '_find_cli', lambda: [sys.executable, str(script)])
    monkeypatch.setattr(cli, '_resolve_backend_cdp', lambda *a, **k: None)
    monkeypatch.setattr(sessions, '_daemon_request', lambda *a: {'ok': True})
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(cli.browser_exec, 'print(1)', task_id='owner', timeout_s=5)
        deadline = time.monotonic() + 3
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        sessions.cleanup_owner('owner')
        assert not sessions.has_owner('owner'), 'cleanup must cancel and drain the CLI'
        assert not json.loads(future.result(timeout=2))['success']


def test_old_cleanup_spares_resume(tmp_path, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from tools import browser_use_sessions as sessions
    from tools import browser_tool

    monkeypatch.setattr(sessions.tempfile, 'gettempdir', lambda: str(tmp_path))
    old_env = {}
    with sessions.lane_call('owner', ''):
        record = sessions.register_lane('owner', '', old_env)
    snapshot = threading.Event()
    release = threading.Event()
    paused_thread = threading.local()
    original_lock = sessions._file_lock
    shutdowns = []
    providers = []

    @contextmanager
    def pause_after_snapshot(path, **kwargs):
        with original_lock(path, **kwargs):
            yield
        if path.name != '.owner.lock' or not getattr(paused_thread, 'pause', False):
            return
        paused_thread.pause = False
        snapshot.set()
        assert release.wait(5)

    def old_cleanup():
        paused_thread.pause = True
        sessions.cleanup_owner('owner')

    monkeypatch.setattr(sessions, '_file_lock', pause_after_snapshot)
    monkeypatch.setattr(sessions, '_daemon_request', lambda *args: shutdowns.append(args) or {'ok': True})
    monkeypatch.setattr(browser_tool, '_cleanup_single_browser_session', providers.append)
    started = tmp_path / 'resumed-started'
    new_env = {}

    def resume():
        with sessions.lane_call('owner', ''):
            assert sessions.register_lane('owner', '', new_env) == record
            return sessions.run_cli(
                [sys.executable, '-c',
                 f"from pathlib import Path; import time; Path({str(started)!r}).touch(); time.sleep(30)"],
                input='', capture_output=True, text=True, timeout=10, env=new_env)

    with ThreadPoolExecutor(2) as pool:
        stale = pool.submit(old_cleanup)
        fresh = None
        try:
            assert snapshot.wait(2)
            sessions.cleanup_owner('owner')
            assert not record.exists()
            fresh = pool.submit(resume)
            deadline = time.monotonic() + 3
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert started.exists()
            assert new_env['BU_NAME'] == old_env['BU_NAME']
            assert new_env['_HERMES_BU_GENERATION'] != old_env['_HERMES_BU_GENERATION']
            fresh_record = record.read_text()
            with sessions._process_lock:
                process = sessions._processes[new_env['BU_NAME']]
            release.set()
            stale.result(timeout=4)

            assert process.poll() is None, 'Old cleanup cancelled the resumed CLI'
            assert record.read_text() == fresh_record, 'Old cleanup removed the resumed record'
            assert (record.parent / '.generation').read_text() == new_env['_HERMES_BU_GENERATION']
            assert len(shutdowns) == len(providers) == 1
        finally:
            release.set()
            stale.result(timeout=4)
            sessions.cleanup_owner('owner')
            if fresh is not None:
                assert fresh.result(timeout=3).returncode != 0

    assert not record.exists(), 'Genuine owner cleanup must still drain the resumed lane'
    assert process.poll() is not None
    assert len(shutdowns) == len(providers) == 2


def test_old_cleanup_rechecks_lane(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from tools import browser_use_sessions as sessions
    from tools import browser_tool

    monkeypatch.setattr(sessions.tempfile, 'gettempdir', lambda: str(tmp_path))
    with sessions.lane_call('owner', ''):
        record = sessions.register_lane('owner', '', {})
    waiting = threading.Event()
    release = threading.Event()
    paused_thread = threading.local()
    original_lock = sessions._file_lock
    shutdowns = []
    providers = []

    @contextmanager
    def pause_before_lane(path, **kwargs):
        if path == record.with_suffix('.lock') and getattr(paused_thread, 'pause', False):
            paused_thread.pause = False
            waiting.set()
            assert release.wait(5)
        with original_lock(path, **kwargs):
            yield

    def old_cleanup():
        paused_thread.pause = True
        sessions.cleanup_owner('owner')

    monkeypatch.setattr(sessions, '_file_lock', pause_before_lane)
    monkeypatch.setattr(sessions, '_daemon_request', lambda *args: shutdowns.append(args) or {'ok': True})
    monkeypatch.setattr(browser_tool, '_cleanup_single_browser_session', providers.append)
    with ThreadPoolExecutor(1) as pool:
        stale = pool.submit(old_cleanup)
        try:
            assert waiting.wait(2)
            sessions.cleanup_owner('owner')
            assert not record.exists()
            env = {}
            with sessions.lane_call('owner', ''):
                assert sessions.register_lane('owner', '', env) == record
            fresh_record = record.read_text()
        finally:
            release.set()
        stale.result(timeout=4)

    assert record.exists(), 'Old cleanup closed the resumed lane after waiting'
    assert record.read_text() == fresh_record
    assert (record.parent / '.generation').read_text() == env['_HERMES_BU_GENERATION']
    assert len(shutdowns) == len(providers) == 1
    sessions.cleanup_owner('owner')
    assert not record.exists()
    assert len(shutdowns) == len(providers) == 2


def test_cleanup_waits_for_shutdown(monkeypatch):
    from tools import browser_use_sessions as sessions
    record = sessions.register_lane('owner', '', {})
    from pathlib import Path
    pid_file = Path(json.loads(record.read_text())['runtime']) / 'bu.pid'
    pid_file.write_text('123')
    waits = []

    def wait(seconds):
        assert record.exists()
        waits.append(seconds)
        if len(waits) == 2:
            pid_file.unlink()

    monkeypatch.setattr(sessions, '_daemon_request', lambda *args: {'ok': True})
    monkeypatch.setattr(sessions.time, 'sleep', wait)
    sessions.cleanup_owner('owner')
    assert len(waits) == 2
    assert not record.exists()


def test_launch_is_atomic_with_close(tmp_path, monkeypatch):
    import threading
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from tools import browser_use_sessions as sessions
    env = {}
    sessions.register_lane('owner', '', env)
    state = Path(env['_HERMES_BU_OWNER_STATE'])
    launching = threading.Event()
    closing = threading.Event()
    original = subprocess.Popen
    observed = []

    def popen(*args, **kwargs):
        launching.set()
        assert closing.wait(2)
        # Cleanup has had an opportunity to close the owner while Popen starts.
        threading.Event().wait(0.1)
        observed.append(state.read_text(encoding='utf-8'))
        return original(*args, **kwargs)

    def close():
        assert launching.wait(2)
        closing.set()
        sessions.cleanup_owner('owner')

    monkeypatch.setattr(sessions.subprocess, 'Popen', popen)
    monkeypatch.setattr(sessions, '_daemon_request', lambda *args: {'ok': True})
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(close)
        sessions.run_cli([sys.executable, '-c', 'pass'], input='', capture_output=True,
                         text=True, timeout=3, env=env)
        pending.result(timeout=3)
    assert observed == [env['_HERMES_BU_GENERATION']]


def test_queued_call_keeps_generation(monkeypatch):
    from contextlib import contextmanager
    from tools import browser_use_sessions as sessions
    record = sessions.register_lane('owner', '', {})
    original_lock = sessions._file_lock

    @contextmanager
    def raced_lock(path, **kwargs):
        if path.name == record.with_suffix('.lock').name:
            # Deterministic boundary: closure after admission, before execution.
            sessions.cleanup_owner('owner')
        with original_lock(path, **kwargs):
            yield

    monkeypatch.setattr(sessions, '_daemon_request', lambda *args: {'ok': True})
    monkeypatch.setattr(sessions, '_file_lock', raced_lock)
    # Avoid recursion when cleanup itself locks the lane.
    def cancel(name):
        monkeypatch.setattr(sessions, '_file_lock', original_lock)
    monkeypatch.setattr(sessions, '_cancel_lane', cancel)
    with pytest.raises(RuntimeError, match='generation|closed'):
        with sessions.lane_call('owner', ''):
            sessions.register_lane('owner', '', {})
    assert not record.exists()


def test_registers_before_failed_exec(tmp_path, monkeypatch):
    from tools import browser_use_sessions as sessions
    monkeypatch.setattr(cli, "_find_cli", lambda: ["fake-browser"])
    monkeypatch.setattr(cli, "_resolve_backend_cdp", lambda *a, **k: None)

    def fail(*args, **kwargs):
        assert sessions.has_owner("owner")
        raise OSError("launch failed")

    monkeypatch.setattr(cli, "_run_cli", fail)
    result = json.loads(cli.browser_exec("print(1)", task_id="task", session_id="owner"))
    assert "launch failed" in result["error"]
    assert sessions.has_owner("owner")
    assert not sessions.has_owner("task")


def test_compression_owner_scope(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from tools import browser_use_sessions as sessions
    from hermes_constants import get_hermes_home
    db = SessionDB(get_hermes_home() / 'state.db')
    try:
        db.create_session('original', 'cli')
        db.end_session('original', 'compression')
        db.create_session('compressed', 'cli', parent_session_id='original')
        db.create_session('fork', 'cli', parent_session_id='original',
                          model_config={'_branched_from': 'original'})
        db.create_session('delegate', 'tool', parent_session_id='original')
        assert sessions.lane_name('compressed') == sessions.lane_name('original')
        assert sessions.lane_name('fork') != sessions.lane_name('original')
        assert sessions.lane_name('delegate') != sessions.lane_name('original')
        record = sessions.register_lane('original', '', {})
        fork = sessions.register_lane('fork', '', {})
        monkeypatch.setattr(sessions, '_daemon_request', lambda *args: {'ok': True})
        sessions.cleanup_owner('compressed')
        assert not record.exists()
        assert fork.exists()
    finally:
        db.close()


def test_lane_names_are_owner_scoped(tmp_path, monkeypatch):
    script = tmp_path / "fake_cli.py"
    script.write_text("import os,sys; sys.stdin.read(); print(os.environ.get('BU_NAME'))")
    monkeypatch.setattr(cli, "_find_cli", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(cli, "_resolve_backend_cdp", lambda *a, **k: None)

    def name(owner, alias=""):
        return json.loads(cli.browser_exec("print(1)", task_id=owner, session=alias))["output"].strip()

    assert name("owner-a") != name("owner-b")
    assert name("owner-a", "research") != name("owner-b", "research")
    assert name("owner-a", "research") == name("owner-a", "research")
    assert name("owner-a") != name("owner-a", "research")
    assert cli._SESSION_RE.fullmatch(name("owner-a", "x" * 64))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "another-profile"))
    other_profile = name("owner-a", "research")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "first-profile"))
    assert other_profile != name("owner-a", "research")
