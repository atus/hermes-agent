"""Exercise the real Browser Use adapter against disposable Chromium."""
import argparse
import json
import shutil
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]
CLI = None
READY_SECONDS = 20
HTTP_SECONDS = 3
EXEC_SECONDS = 30
POLL_SECONDS = 0.1
RESULT_PREFIX = 'E2E_RESULT='


def pages(base):
    with urllib.request.urlopen(base + '/json/list', timeout=HTTP_SECONDS) as response:
        return {row['id']: row for row in json.load(response) if row['type'] == 'page'}


def run_case(root, base):
    os.environ['HERMES_HOME'] = str(root / 'hermes')
    os.environ['BH_HOME'] = str(root / 'harness')
    os.environ['BH_RUNTIME_DIR'] = str(root / 'ipc')
    os.environ['BH_RUNTIME_DIR_SHARED'] = '1'
    os.environ['BH_TMP_DIR'] = str(root / 'tmp')
    os.environ['BH_TMP_DIR_SHARED'] = '1'
    os.environ['BU_CDP_URL'] = base
    os.environ['BROWSER_CDP_URL'] = base
    os.environ['ANONYMIZED_TELEMETRY'] = 'false'
    for key in ('BU_NAME', 'BU_CDP_WS', 'BU_AUTOSPAWN', 'BH_AGENT_WORKSPACE'):
        os.environ.pop(key, None)
    home = root / 'hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('browser:\n  backend: browser-use\n  cdp_url: ' + base + '\n')
    sys.path.insert(0, str(REPO))
    from tools import browser_use_cli, browser_tool
    browser_use_cli._find_cli = lambda: [CLI]

    call_numbers = {}

    def invoke(owner, code, lane='research'):
        call_numbers[owner] = call_numbers.get(owner, 0) + 1
        task = f"turn-{call_numbers[owner]}-{owner}"
        result = browser_use_cli.browser_exec(code, task_id=task, session_id=owner, session=lane, timeout_s=EXEC_SECONDS)
        data = json.loads(result) if isinstance(result, str) else result
        assert data.get('success'), data
        output = data.get('output', '')
        records = [line[len(RESULT_PREFIX):] for line in output.splitlines() if line.startswith(RESULT_PREFIX)]
        return json.loads(records[-1]) if records else output

    baseline = set(pages(base))
    report = {'baseline_pages': len(baseline), 'checks': []}
    try:
        a = invoke('owner-a', "goto_url('data:text/html,<title>A</title>'); print('E2E_RESULT='+__import__('json').dumps(current_tab()))")
        aid = a.get('targetId') or a['target_id']
        assert aid not in baseline, 'Owner A adopted an unowned baseline tab'
        a_initial = set(pages(base)) - baseline
        b = invoke('owner-b', "goto_url('data:text/html,<title>B</title>'); print('E2E_RESULT='+__import__('json').dumps(current_tab()))")
        bid = b.get('targetId') or b['target_id']
        assert bid != aid and bid not in baseline, 'Same alias collides across owners'
        b_initial = set(pages(base)) - baseline - a_initial
        report['initial_pages_per_lane'] = {'a': len(a_initial), 'b': len(b_initial)}
        report['checks'].append('same lane isolated between owners')
        count = len(pages(base))
        again = invoke('owner-a', "goto_url('data:text/html,<title>A-reused</title>'); print('E2E_RESULT='+__import__('json').dumps(current_tab()))")
        assert (again.get('targetId') or again['target_id']) == aid
        assert len(pages(base)) == count, 'Sequential navigation created tabs'
        report['checks'].append('navigation reuses owned tab')
        extra = invoke('owner-a', "new_tab('data:text/html,<title>A-extra</title>'); print('E2E_RESULT='+__import__('json').dumps(current_tab()))")
        extra_id = extra.get('targetId') or extra['target_id']
        assert extra_id != aid and len(pages(base)) == count + 1
        report['checks'].append('explicit new tab retained alongside original')
        invoke('owner-a', 'close_tab()')
        assert extra_id not in pages(base) and bid in pages(base)
        report['checks'].append('explicit close removes only owned tab')
        invoke('owner-a', "goto_url('data:text/html,<title>A-recovered</title>')")
        assert baseline | {bid} <= set(pages(base))
        report['checks'].append('closed current tab recovers without taking B')
        raw = invoke('owner-a', "print('E2E_RESULT='+__import__('json').dumps(cdp('Target.createTarget', url='about:blank', background=True)))")
        raw_id = raw['targetId']
        assert raw_id in pages(base)
        owned = invoke('owner-a', "print('E2E_RESULT='+__import__('json').dumps(list_tabs()))")
        assert raw_id in {tab['targetId'] for tab in owned}, 'Raw CDP creation was not registered'
        assert bid not in {tab['targetId'] for tab in owned}, 'Scoped listing exposed sibling tab'
        report['checks'].append('raw CDP create is owned; listing is scoped')
        before_popup = set(pages(base))
        invoke('owner-a', "cdp('Runtime.evaluate', expression=\"window.open('about:blank', '_blank')\", userGesture=True)")
        popup_ids = set(pages(base)) - before_popup
        assert len(popup_ids) == 1, 'Test popup did not open exactly once'
        owned = invoke('owner-a', "print('E2E_RESULT='+__import__('json').dumps(list_tabs()))")
        assert popup_ids <= {tab['targetId'] for tab in owned}, 'Opener-owned popup was not registered'
        report['checks'].append('popup descendant joins owner set')
        before_unlisted = set(pages(base))
        invoke('owner-a', "cdp('Runtime.evaluate', expression=\"window.open('about:blank', '_blank')\", userGesture=True)")
        assert len(set(pages(base)) - before_unlisted) == 1
        # Close the opener before listing again: its unlisted popup must stay owned.
        invoke('owner-a', 'close_tab()')
        browser_tool.cleanup_browser('owner-a')
        remaining = pages(base)
        assert baseline | {bid} <= set(remaining), 'Owner cleanup closed sentinel or B'
        assert aid not in remaining, 'Owner A leaked its original tab'
        assert set(remaining) == baseline | b_initial, {
            'error': 'Owner A leaked tabs or cleanup damaged B',
            'baseline': sorted(baseline), 'a_initial': sorted(a_initial),
            'b_initial': sorted(b_initial), 'remaining': remaining,
        }
        report['checks'].append('owner cleanup preserves unrelated tabs')
        browser_tool.cleanup_browser('owner-a')
        browser_tool.cleanup_browser('owner-b')
        assert set(pages(base)) == baseline, 'Cleanup failed to return to baseline'
        report['checks'].append('all owners cleaned; baseline restored; idempotent')
        from concurrent.futures import ThreadPoolExecutor
        ready = root / 'race-ready'
        release = root / 'race-release'
        race_code = (
            "from pathlib import Path\nimport time\n"
            f"Path({str(ready)!r}).write_text('ready')\n"
            f"while not Path({str(release)!r}).exists():\n    time.sleep(0.05)\n"
            "new_tab('data:text/html,<title>LATE</title>')"
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(browser_use_cli.browser_exec, race_code,
                                      task_id='race-turn', session_id='owner-race',
                                      session='research', timeout_s=EXEC_SECONDS)
            deadline = time.monotonic() + READY_SECONDS
            while not ready.exists() and time.monotonic() < deadline:
                assert not pending.done(), pending.result()
                time.sleep(POLL_SECONDS)
            assert ready.exists(), 'Race fixture did not start'
            browser_tool.cleanup_browser('owner-race')
            assert set(pages(base)) == baseline, 'Race owner not closed'
            try:
                invoke('owner-race', "goto_url('data:text/html,<title>RESUMED</title>')")
                resumed_ids = set(pages(base)) - baseline
                assert resumed_ids, 'Resumed owner could not reopen browser'
            finally:
                release.write_text('release')
            outcome = pending.result(timeout=EXEC_SECONDS)
            outcome = json.loads(outcome) if isinstance(outcome, str) else outcome
            assert set(pages(base)) == baseline | resumed_ids, 'Old exec resurrected a tab in the resumed owner'
            assert not outcome.get('success'), 'Old generation continued after owner cleanup'
        report['checks'].append('in-flight execution cannot resurrect closed-owner tabs')
        browser_tool.cleanup_browser('owner-race')
        assert set(pages(base)) == baseline
        report['checks'].append('resumed session gets fresh resource generation')
        report['final_pages'] = len(pages(base))
        print(json.dumps(report, indent=2))
    finally:
        # Only this disposable harness runtime and browser are ever touched.
        for owner in ('owner-a', 'owner-b', 'owner-race'):
            browser_tool.cleanup_browser(owner)
        env = dict(os.environ)
        for pid_file in (root / 'ipc').glob('*.pid'):
            env['BU_NAME'] = pid_file.stem.removeprefix('bu-')
            subprocess.run([CLI, '--reload'], env=env, capture_output=True, timeout=10)
        for record in (home / 'cache' / 'browser-use' / 'owners').glob('*/*.json'):
            data = json.loads(record.read_text())
            if not data.get('runtime') or not data.get('name'):
                continue
            owned_env = dict(env, BU_NAME=data['name'], BH_RUNTIME_DIR=data['runtime'], BH_RUNTIME_DIR_SHARED='0')
            subprocess.run([CLI, '--reload'], env=owned_env, capture_output=True, timeout=10)


def main():
    global CLI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-use', default=shutil.which('browser-use'))
    parser.add_argument('--chromium', type=Path)
    args = parser.parse_args()
    CLI = args.browser_use
    if os.name != 'posix':
        parser.error('This disposable process-group smoke test requires POSIX')
    if not CLI:
        parser.error('Provide --browser-use pointing to the installed CLI')
    chrome = args.chromium
    if chrome is None:
        candidates = list((Path.home() / '.cache' / 'ms-playwright').glob('chromium-*/chrome-linux64/chrome'))
        if not candidates:
            parser.error('Provide --chromium pointing to a Chromium executable')
        chrome = max(candidates, key=lambda path: int(path.parents[1].name.removeprefix('chromium-')))

    with tempfile.TemporaryDirectory(prefix='bt-') as directory:
        root = Path(directory)
        profile = root / 'chrome'
        profile.mkdir()
        with (root / 'chrome.log').open('w') as log:
            process = subprocess.Popen([
                str(chrome), '--headless', '--no-sandbox', '--disable-gpu',
                '--no-first-run', '--no-default-browser-check',
                '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
                '--user-data-dir=' + str(profile),
                'data:text/html,<title>UNOWNED-SENTINEL</title>'
            ], stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + READY_SECONDS
                port_file = profile / 'DevToolsActivePort'
                while time.monotonic() < deadline:
                    assert process.poll() is None, (root / 'chrome.log').read_text()
                    if port_file.exists():
                        base = 'http://127.0.0.1:' + port_file.read_text().splitlines()[0]
                        try:
                            if pages(base):
                                break
                        except OSError:
                            pass
                    time.sleep(POLL_SECONDS)
                else:
                    raise RuntimeError('Disposable Chrome readiness timed out')
                run_case(root, base)
            finally:
                # This process group was created solely for disposable Chrome.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
