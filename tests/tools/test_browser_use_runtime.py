"""Behavioral CDP double: no Chromium or production daemon is contacted."""
import json
import pytest
from types import SimpleNamespace


def _harness():
    tabs = {"user": {"targetId": "user", "type": "page", "url": "https://user", "title": "User"}}
    state = {"current": "user", "epoch": "browser-1", "created": 0, "calls": []}
    sessions = {}

    def cdp(method, session_id=None, **params):
        state["calls"].append((method, session_id, params))
        if method == "Target.getTargetInfo":
            target = params.get("targetId")
            return {"targetInfo": dict(tabs[target]) if target else {"targetId": state["epoch"], "type": "browser"}}
        if method == "Target.getTargets":
            return {"targetInfos": list(tabs.values())}
        if method == "Target.createTarget":
            state["created"] += 1
            target = f"owned-{state['created']}"
            tabs[target] = {"targetId": target, "type": "page", "url": params["url"], "title": ""}
            return {"targetId": target}
        if method == "Target.attachToTarget":
            sid = f"sid-{params['targetId']}"
            sessions[sid] = params["targetId"]
            return {"sessionId": sid}
        if method == "Target.closeTarget":
            tabs.pop(params["targetId"], None)
            return {"success": True}
        if method == "Page.navigate":
            tabs[sessions.get(session_id, state["current"])]["url"] = params["url"]
            return {}
        return {}

    def send(request):
        if request["meta"] == "set_session":
            state["current"] = request["target_id"]
        return {}

    helpers = SimpleNamespace(cdp=cdp, _send=send)
    helpers.goto_url = lambda url: helpers.cdp("Page.navigate", url=url)
    return helpers, tabs, state


def test_raw_create_records_target(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    record = tmp_path / 'lane.tabs.json'
    runtime.install_scope(helpers, record)
    target = helpers.cdp('Target.createTarget', url='https://raw')['targetId']
    assert target in json.loads(record.read_text())['targets']
    helpers.close_tab(target)
    assert target not in tabs


def test_popup_opener_descendants(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    record = tmp_path / 'lane.tabs.json'
    runtime.install_scope(helpers, record)
    owner = helpers.current_tab()['targetId']
    for name, opener in [('child', owner), ('grandchild', 'child'), ('foreign', 'user')]:
        tabs[name] = {'targetId': name, 'openerId': opener, 'type': 'page'}
    assert {tab['targetId'] for tab in helpers.list_tabs()} == {owner, 'child', 'grandchild'}
    assert set(json.loads(record.read_text())['targets']) == {owner, 'child', 'grandchild'}


@pytest.mark.parametrize('operation', ['close', 'recover'])
def test_popup_ancestry_survives(tmp_path, monkeypatch, operation):
    from tools import browser_use_runtime as runtime
    from tools import browser_use_sessions as sessions
    from tools import browser_tool

    env = {}
    record = sessions.register_lane('popup-owner', '', env)
    journal = record.with_suffix('.tabs.json')
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    helpers, tabs, state = _harness()
    raw = helpers.cdp
    runtime.install_scope(helpers, journal)
    parent = helpers.current_tab()['targetId']
    for name, opener in [('grandchild', 'popup'), ('popup', parent), ('foreign', 'user')]:
        tabs[name] = {'targetId': name, 'openerId': opener, 'type': 'page'}

    if operation == 'close':
        helpers.close_tab(parent)
    else:
        del tabs[parent]
        recovered = helpers.ensure_real_tab()
        assert recovered['targetId'] in {'popup', 'grandchild'}

    assert parent not in tabs
    assert set(json.loads(journal.read_text())['targets']) == {'popup', 'grandchild'}
    assert {tab['targetId'] for tab in helpers.list_tabs()} == {'popup', 'grandchild'}

    def request(runtime_path, payload):
        if payload.get('meta') == 'shutdown':
            return {'ok': True}
        return {'result': raw(payload['method'], **payload.get('params', {}))}

    monkeypatch.setattr(sessions, '_daemon_request', request)
    monkeypatch.setattr(browser_tool, '_cleanup_single_browser_session', lambda name: None)
    sessions.cleanup_owner('popup-owner')
    assert set(tabs) == {'user', 'foreign'}
    assert not record.exists()
    assert not journal.exists()


@pytest.mark.parametrize('method, params', [
    ('Browser.close', {}),
    ('Target.disposeBrowserContext', {'browserContextId': 'foreign'}),
])
def test_foreign_raw_routes_fail(tmp_path, method, params):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    runtime.install_scope(helpers, tmp_path / 'lane.tabs.json')
    before = len(state['calls'])
    with pytest.raises(ValueError, match='owned|browser-wide'):
        helpers.cdp(method, **params)
    assert len(state['calls']) == before


def test_explicit_frame_cdp_preserved(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    runtime.install_scope(helpers, tmp_path / 'lane.tabs.json')
    page = helpers.current_tab()['targetId']
    frame = helpers.cdp('Target.attachToTarget', targetId='oopif', flatten=True)['sessionId']
    helpers.cdp('Runtime.evaluate', session_id=frame, expression='document.title')
    assert state['calls'][-1] == ('Runtime.evaluate', frame, {'expression': 'document.title'})
    assert helpers.current_tab()['targetId'] == page


def test_close_failure_retains_target(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    raw = helpers.cdp
    helpers.cdp = lambda method, **kwargs: {'success': False} if method == 'Target.closeTarget' else raw(method, **kwargs)
    record = tmp_path / 'lane.tabs.json'
    runtime.install_scope(helpers, record)
    target = helpers.current_tab()['targetId']
    assert helpers.close_tab() == {'success': False}
    assert target in json.loads(record.read_text())['targets']
    assert helpers.current_tab()['targetId'] == target


def test_closed_generation_cannot_resume(tmp_path, monkeypatch):
    from tools import browser_use_runtime as runtime
    from tools import browser_use_sessions as sessions
    env = {}
    record = sessions.register_lane('owner', '', env)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    helpers, tabs, state = _harness()
    scope = runtime.install_scope(helpers, record.with_suffix('.tabs.json'))
    monkeypatch.setattr(sessions, '_daemon_request', lambda *args: (_ for _ in ()).throw(OSError('offline')))
    sessions.cleanup_owner('owner')
    before = len(state['calls'])
    with pytest.raises(RuntimeError, match='closed|generation'):
        helpers.new_tab()
    with pytest.raises(RuntimeError, match='closed|generation'):
        scope._save()
    assert len(state['calls']) == before
    assert record.exists(), 'failed cleanup must remain retryable'


@pytest.mark.parametrize('operation', ['helper', 'raw'])
def test_create_racing_closure(tmp_path, monkeypatch, operation):
    from tools import browser_use_runtime as runtime
    generation = tmp_path / 'generation'
    generation.write_text('open', encoding='utf-8')
    monkeypatch.setenv('_HERMES_BU_OWNER_STATE', str(generation))
    monkeypatch.setenv('_HERMES_BU_GENERATION', 'open')
    helpers, tabs, state = _harness()
    raw = helpers.cdp
    scope = runtime.install_scope(helpers, tmp_path / 'lane.tabs.json')
    before = set(tabs)

    def racing_cdp(method, **params):
        result = raw(method, **params)
        if method == 'Target.createTarget':
            generation.write_text('closed', encoding='utf-8')
        return result

    scope._send_cdp = racing_cdp
    with pytest.raises(RuntimeError, match='generation|closed'):
        if operation == 'helper':
            helpers.new_tab()
        else:
            helpers.cdp('Target.createTarget', url='about:blank')
    assert set(tabs) == before, 'Late create response leaked an unjournaled tab'


def test_first_exec_owns_and_reuses_tab(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    record = tmp_path / "lane.tabs.json"
    scope = runtime.install_scope(helpers, record)
    initial = scope.current_tab()["targetId"]
    helpers.goto_url("https://first")
    helpers.goto_url("https://second")
    assert len(tabs) == 2
    assert tabs[initial]["url"] == "https://second"
    assert tabs["user"]["url"] == "https://user"
    assert all(sid is not None for method, sid, params in state["calls"] if method == "Page.navigate")
    saved = json.loads(record.read_text())
    assert saved["targets"] == [initial]
    assert saved["epoch"] == "browser-1"


def test_recovery_returns_owned_info(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    runtime.install_scope(helpers, tmp_path / 'lane.tabs.json')
    tab = helpers.ensure_real_tab()
    assert tab['targetId'] != 'user'
    del tabs[tab['targetId']]
    recovered = helpers.ensure_real_tab()
    assert recovered['targetId'] not in {tab['targetId'], 'user'}
    assert recovered['target_id'] == recovered['targetId']


def test_explicit_tab_lifecycle(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    record = tmp_path / "lane.tabs.json"
    scope = runtime.install_scope(helpers, record)
    first = scope.current_tab()["targetId"]
    second = helpers.new_tab("https://compare")
    assert first != second
    assert {tab["targetId"] for tab in helpers.list_tabs()} == {first, second}
    helpers.switch_tab(first)
    helpers.goto_url("https://first")
    assert tabs[first]["url"] == "https://first"
    assert tabs[second]["url"] == "https://compare"
    helpers.close_tab(second)
    assert json.loads(record.read_text())["targets"] == [first]
    helpers.close_tab()
    assert set(tabs) == {"user"}
    assert json.loads(record.read_text())["targets"] == []


def test_foreign_tab_operations_fail(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    runtime.install_scope(helpers, tmp_path / "lane.tabs.json")
    for operation in (helpers.switch_tab, helpers.close_tab):
        with pytest.raises(ValueError, match="owned"):
            operation("user")
    with pytest.raises(ValueError, match="owned"):
        helpers.cdp("Target.closeTarget", targetId="user")
    assert tabs["user"]["url"] == "https://user"


def test_reconnect_and_recovery(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    raw_cdp = helpers.cdp
    record = tmp_path / "lane.tabs.json"
    runtime.install_scope(helpers, record)
    first = helpers.current_tab()["targetId"]
    second = helpers.new_tab("https://second")
    helpers.cdp = raw_cdp  # fresh CLI interpreter, same browser
    runtime.install_scope(helpers, record)
    assert helpers.current_tab()["targetId"] == second
    assert len(tabs) == 3
    del tabs[second]
    helpers.ensure_real_tab()
    assert helpers.current_tab()["targetId"] == first
    del tabs[first]
    helpers.ensure_real_tab()
    replacement = helpers.current_tab()["targetId"]
    assert replacement not in {"user", first, second}
    assert tabs["user"]["url"] == "https://user"


def test_browser_restart_drops_old_ids(tmp_path):
    from tools import browser_use_runtime as runtime
    helpers, tabs, state = _harness()
    raw_cdp = helpers.cdp
    record = tmp_path / "lane.tabs.json"
    runtime.install_scope(helpers, record)
    old = helpers.current_tab()["targetId"]
    state["epoch"] = "browser-2"
    helpers.cdp = raw_cdp
    runtime.install_scope(helpers, record)
    assert old not in {tab["targetId"] for tab in helpers.list_tabs()}
    assert json.loads(record.read_text())["epoch"] == "browser-2"


def test_adapter_installs_scope(tmp_path, monkeypatch):
    import contextlib
    import io
    import os
    import sys
    from tools import browser_use_cli as cli
    from tools import browser_use_sessions as sessions
    helpers, tabs, state = _harness()
    monkeypatch.setitem(sys.modules, "browser_harness", SimpleNamespace(helpers=helpers))
    monkeypatch.setattr(cli, "_find_cli", lambda: ["fake"])
    monkeypatch.setattr(cli, "_resolve_backend_cdp", lambda *a, **k: None)

    def execute(*args, **kwargs):
        with monkeypatch.context() as child:
            for key, value in kwargs["env"].items():
                child.setenv(key, value)
            namespace = {"goto_url": helpers.goto_url}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(kwargs["input"], namespace)
            return SimpleNamespace(stdout=output.getvalue(), stderr="", returncode=0)

    monkeypatch.setattr(cli, "_run_cli", execute)
    result = json.loads(cli.browser_exec("goto_url('https://example.com')", task_id="owner"))
    assert result["success"]
    assert tabs["user"]["url"] == "https://user"
    assert len(tabs) == 2
