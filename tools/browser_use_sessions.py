"""Owner-scoped Browser Use lanes and lifecycle bookkeeping."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import time
import json
import os
from pathlib import Path
import tempfile
import subprocess
import threading
from uuid import uuid4

from hermes_constants import get_hermes_home

_registered = set()
_processes = {}
_process_lock = threading.Lock()
_CANCEL_SECONDS = 1
_IPC_TIMEOUT = 3
_MAX_IPC_BYTES = 8 * 1024 * 1024


_LOCK_TIMEOUT = 30
_LOCK_POLL_SECONDS = 0.02


@contextmanager
def _file_lock(path, timeout=_LOCK_TIMEOUT):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Browser lane is still executing')
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_admission = ContextVar('browser_lane_admission', default=None)


@contextmanager
def lane_call(owner, alias):
    """Capture admission before waiting, so queued calls cannot resurrect owners."""
    directory = _owner_dir(owner)
    state = directory / '.generation'
    with _file_lock(directory / '.owner.lock'):
        generation = state.read_text(encoding='utf-8') if state.exists() else ''
        if generation == 'closed' and has_owner(owner):
            raise RuntimeError('Browser owner generation is closed; cleanup pending')
        if generation in {'', 'closed'}:
            generation = uuid4().hex
            _write_atomic(state, generation)
    token = _admission.set((state, generation))
    try:
        with _file_lock(directory / (lane_name(owner, alias) + '.lock')):
            if state.read_text(encoding='utf-8') != generation:
                raise RuntimeError('Browser owner generation is closed')
            yield
    finally:
        _admission.reset(token)


def run_cli(cmd, *, input, capture_output, text, timeout, env, **kwargs):
    """Track the exact CLI process so owner teardown can cancel/drain it."""
    name = env['BU_NAME']
    state = Path(env['_HERMES_BU_OWNER_STATE'])
    with _file_lock(state.parent / '.owner.lock'), _process_lock:
        if state.read_text(encoding='utf-8') != env['_HERMES_BU_GENERATION']:
            raise RuntimeError('Browser owner generation is closed')
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=text, env=env, **kwargs)
        _processes[name] = process
    try:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    finally:
        with _process_lock:
            _processes.pop(name, None)


def _cancel_lane(name):
    with _process_lock:
        process = _processes.get(name)
        if process is None or process.poll() is not None:
            return
        process.terminate()
    try:
        process.wait(timeout=_CANCEL_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_CANCEL_SECONDS)


def _daemon_request(runtime, payload):
    """Speak the harness newline JSON protocol; never launch a daemon."""
    import socket

    runtime = Path(runtime)
    if os.name == 'nt':
        address = json.loads((runtime / 'bu.port').read_text(encoding='utf-8'))
        connection = socket.create_connection(('127.0.0.1', int(address['port'])), _IPC_TIMEOUT)
        payload = {**payload, 'token': address['token']}
    else:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(_IPC_TIMEOUT)
        try:
            connection.connect(str(runtime / 'bu.sock'))
        except BaseException:
            connection.close()
            raise

    with connection:
        connection.settimeout(_IPC_TIMEOUT)
        connection.sendall((json.dumps(payload) + '\n').encode())
        data = bytearray()
        while not data.endswith(b'\n'):
            chunk = connection.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _MAX_IPC_BYTES:
                raise ValueError('Browser cleanup IPC response too large')
    response = json.loads(data)
    if not isinstance(response, dict) or response.get('error'):
        raise RuntimeError(f'Browser cleanup failed: {response}')
    return response


def _close_lane(record):
    data = json.loads(record.read_text(encoding='utf-8'))
    runtime = data['runtime']
    tabs_file = record.with_suffix('.tabs.json')
    saved = json.loads(tabs_file.read_text(encoding='utf-8')) if tabs_file.exists() else {}
    if saved.get('epoch'):
        epoch = _daemon_request(runtime, {'method': 'Target.getTargetInfo'})['result']['targetInfo']['targetId']
        if epoch == saved['epoch']:
            live = _daemon_request(runtime, {'method': 'Target.getTargets'})['result']['targetInfos']
            live_ids = {tab['targetId'] for tab in live}
            # Capture opener descendants before closing parents, including
            # popups opened after the last helper call. Persist for retries.
            targets = saved.setdefault('targets', [])
            while True:
                children = [tab['targetId'] for tab in live if tab.get('type') == 'page'
                            and tab.get('openerId') in targets and tab['targetId'] not in targets]
                if not children:
                    break
                targets.extend(children)
            tabs_file.write_text(json.dumps(saved), encoding='utf-8')
            for target in targets:
                if target not in live_ids:
                    continue
                result = _daemon_request(runtime, {'method': 'Target.closeTarget', 'params': {'targetId': target}})
                if not result['result'].get('success'):
                    raise RuntimeError(f'Could not close owned tab {target}')
            # Close acknowledgement can precede target disappearance.
            deadline = time.monotonic() + _IPC_TIMEOUT
            while True:
                remaining = _daemon_request(runtime, {'method': 'Target.getTargets'})['result']['targetInfos']
                if not set(targets) & {tab['targetId'] for tab in remaining}:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Owned tabs remain after cleanup')
                time.sleep(_LOCK_POLL_SECONDS)

    response = _daemon_request(runtime, {'meta': 'shutdown'})
    if response.get('ok') is not True:
        raise RuntimeError('Browser daemon did not acknowledge shutdown')
    # The ACK precedes the daemon's finally block closing its bootstrap tab.
    deadline = time.monotonic() + _IPC_TIMEOUT
    while (Path(runtime) / 'bu.pid').exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Browser daemon shutdown is still pending')
        time.sleep(_LOCK_POLL_SECONDS)
    tabs_file.unlink(missing_ok=True)
    record.unlink(missing_ok=True)


def cleanup_owner(owner):
    """Close exact owned targets and their existing daemons; retain failures."""
    import logging

    directory = _owner_dir(owner)
    with _file_lock(directory / '.owner.lock'):
        state = directory / '.generation'
        _write_atomic(state, 'closed')
        records = []
        for record in directory.glob('*.json'):
            if record.name.endswith('.tabs.json'):
                continue
            try:
                records.append((record, record.read_text(encoding='utf-8')))
            except (OSError, ValueError) as error:
                logging.getLogger(__name__).warning('Browser cleanup retained %s: %s', record.name, error)
    for record, snapshot in records:
        try:
            # Records include the generation: a reused path/name is not identity.
            with _file_lock(directory / '.owner.lock'):
                if not record.exists() or record.read_text(encoding='utf-8') != snapshot:
                    continue
                _cancel_lane(record.stem)
            # Launch takes lane -> owner; release owner before waiting for lane.
            with (
                _file_lock(record.with_suffix('.lock'), timeout=_IPC_TIMEOUT),
                _file_lock(directory / '.owner.lock'),
            ):
                if not record.exists() or record.read_text(encoding='utf-8') != snapshot:
                    continue
                name = json.loads(snapshot)['name']
                _close_lane(record)
                from tools.browser_tool import _cleanup_single_browser_session

                _cleanup_single_browser_session(name)
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            logging.getLogger(__name__).warning('Browser cleanup retained %s: %s', record.name, error)


def cleanup_all_owners():
    """Only owners registered by this process in the active profile."""
    profile = str(get_hermes_home())
    for home, owner in list(_registered):
        if home == profile:
            cleanup_owner(owner)


_owner_roots = {}
_OWNER_CACHE_LIMIT = 1024


def resolve_owner(owner):
    """Compression-only lineage, shared by execution and cleanup; never fork roots."""
    owner = str(owner or 'default')
    path = get_hermes_home() / 'state.db'
    if not path.exists():
        return owner
    key = (str(path.resolve()), owner)
    if key in _owner_roots:
        return _owner_roots[key]
    try:
        from hermes_state import SessionDB

        db = SessionDB(path, read_only=True)
        try:
            lineage = db.get_compression_lineage(owner)
        finally:
            db.close()
        if not lineage:
            return owner  # The session row may not have been persisted yet.
        if len(_owner_roots) >= _OWNER_CACHE_LIMIT:
            _owner_roots.clear()
        _owner_roots[key] = lineage[0]
        return lineage[0]
    except Exception:
        return owner


def lane_name(task_id, alias=""):
    """Stable, bounded harness name, isolated by profile and owner."""
    owner = f"{get_hermes_home().resolve()}\0{resolve_owner(task_id)}\0{alias}"
    return "hermes-" + sha256(owner.encode()).hexdigest()[:40]


def _owner_dir(owner):
    key = lane_name(owner)
    return get_hermes_home() / "cache" / "browser-use" / "owners" / key


def has_owner(owner):
    """Whether this profile has registered lanes for a session."""
    return any(_owner_dir(owner).glob("*.json"))


def _write_atomic(path, text):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def register_lane(owner, alias, env):
    directory = _owner_dir(owner)
    with _file_lock(directory / '.owner.lock'):
        return _register_lane(owner, alias, env)


def _register_lane(owner, alias, env):
    """Register before provider provisioning or CLI launch, including failures."""
    name = lane_name(owner, alias)
    directory = _owner_dir(owner)
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / '.generation'
    generation = state.read_text(encoding='utf-8') if state.exists() else ''
    admitted = _admission.get()
    if admitted is not None and admitted != (state, generation):
        raise RuntimeError('Browser owner generation is closed')
    if generation == 'closed' and has_owner(owner):
        raise RuntimeError('Browser owner is closed; cleanup must complete before resume')
    if generation in {'', 'closed'}:
        generation = uuid4().hex
        _write_atomic(state, generation)
    env.update(_HERMES_BU_OWNER_STATE=str(state), _HERMES_BU_GENERATION=generation)
    runtime = Path(tempfile.gettempdir()) / f"hermes-bu-{os.getuid() if hasattr(os, 'getuid') else 'user'}" / name
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = directory / f"{name}.json"
    _write_atomic(record, json.dumps({"name": name, "alias": alias, "runtime": str(runtime), "generation": generation}))
    _registered.add((str(get_hermes_home()), owner))
    env.update(BU_NAME=name, BH_RUNTIME_DIR=str(runtime), BH_RUNTIME_DIR_SHARED="0")
    return record
