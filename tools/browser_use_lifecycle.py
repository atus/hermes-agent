"""Profile/session-scoped ownership for browser-harness (not legacy endpoints)."""
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path

from hermes_constants import get_hermes_home


def _private_dir(path, mode=0o700):
    path.mkdir(mode=mode, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"Not a private directory: {path}")
    forbidden = 0o077 if mode == 0o700 else 0o022
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & forbidden):
        raise OSError(f"Unsafe browser ownership directory: {path}")
    return path


def _state_dir():
    path = Path(get_hermes_home()).resolve()
    for part in ("cache", "browser-use", "owned"):
        path = _private_dir(path / part, 0o700 if part == "owned" else 0o755)
    return path


@contextlib.contextmanager
def _parent_fd(path):
    """Walk without following any ancestor; pin rename/open to that directory."""
    if os.name == "nt":
        if any(parent.is_symlink() for parent in path.parents):
            raise OSError("Browser state path has a symlink ancestor")
        yield None
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _record_id(home, owner, name):
    return hashlib.sha256(json.dumps([str(home), owner, name]).encode()).hexdigest()


def _runtime_dir(key):
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    root = _private_dir(Path(tempfile.gettempdir()) / f"hb-{uid}")
    path = root / key[:24]
    if os.name != "nt" and len(os.fsencode(path / "bu.sock")) >= 104:
        raise OSError("Browser runtime path exceeds the Unix socket budget")
    return _private_dir(path)


def _write_record(path, record):
    with _parent_fd(path) as parent:
        name = ".owned-" + secrets.token_hex(16)
        target = path.parent / name if parent is None else name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            destination = path if parent is None else path.name
            os.replace(target, destination, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(target, dir_fd=parent)


_homes = set()
_lease_fd: ContextVar[int | None] = ContextVar("harness_execution_lease_fd", default=None)
_logger = logging.getLogger(__name__)


def _safe_open(path, flags):
    if path.is_symlink():
        raise OSError(f"Refusing browser state symlink: {path}")
    with _parent_fd(path) as parent:
        target = path if parent is None else path.name
        flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(target, flags | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise OSError("Browser ownership state must be a regular single-link file")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        os.close(fd)
        raise OSError("Unsafe browser ownership file permissions")
    return fd


@contextlib.contextmanager
def _lease(path, timeout=0):
    fd = _safe_open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"1")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Browser session is executing or being cleaned up; retry later") from None
                time.sleep(0.05)
        token = _lease_fd.set(fd)
        try:
            yield
        finally:
            _lease_fd.reset(token)
    finally:
        os.close(fd)  # OS leases disappear when the last executing holder exits.


def _inherit_lease() -> dict:
    fd = _lease_fd.get()
    return {"pass_fds": (fd,)} if os.name != "nt" and fd is not None else {}


def _read_record(path):
    with os.fdopen(_safe_open(path, os.O_RDONLY), encoding="utf-8-sig") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("Oversized browser ownership record")
    record = json.loads(raw)
    home = str(Path(get_hermes_home()).resolve())
    owner, name = record["owner"], record["name"]
    if not isinstance(owner, str) or not 0 < len(owner) <= 512:
        raise ValueError("Invalid browser owner")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Invalid browser name")
    key = _record_id(home, owner, name)
    if record["version"] != 1 or record["home"] != home or path.name != f"{key}.json":
        raise ValueError("Browser record does not belong to this profile/session")
    if record["runtime"] != str(_runtime_dir(key)):
        raise ValueError("Browser runtime does not match its owner")
    stamp = record["last_execution"]
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
        raise ValueError("Invalid browser execution timestamp")
    _validate_identity(record)
    return record


def _validate_identity(record):
    pid, start = record.get("pid"), record.get("start")
    if pid is not None:
        if type(pid) is not int or not 0 < pid < (1 << 31):
            raise ValueError("Invalid harness PID")
        if type(start) not in (int, float) or not math.isfinite(start) or start <= 0:
            raise ValueError("Invalid harness generation")
    if record.get("cleanup") not in {"active", "detached", "retry"}:
        raise ValueError("Invalid harness cleanup state")
    endpoint = record.get("endpoint")
    if endpoint is not None and (not isinstance(endpoint, str) or len(endpoint) > 8192
                                 or not endpoint.startswith(("http://", "https://", "ws://", "wss://"))):
        raise ValueError("Invalid owned CDP endpoint")


def _scope_env(env, home, name, runtime):
    config = _private_dir(Path(home) / "cache" / "browser-use" / "harness")
    env.update(BU_NAME=name, BH_RUNTIME_DIR=str(runtime), BH_RUNTIME_DIR_SHARED="0",
               BH_TMP_DIR=str(runtime), BH_TMP_DIR_SHARED="0", BH_HOME=str(config), BH_CONFIG_DIR=str(config))


@contextlib.contextmanager
def _execution(env, owner, name):
    home = Path(get_hermes_home()).resolve()
    owner = str(owner or "default")
    name = name or "default"
    if len(owner) > 512 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Invalid browser owner or session name")
    key = _record_id(home, owner, name)
    path = _state_dir() / f"{key}.json"
    runtime = _runtime_dir(key)
    _scope_env(env, home, name, runtime)
    with _lease(path, timeout=5):
        if not path.exists() and any(runtime.joinpath(f"bu.{suffix}").exists()
                                     for suffix in ("pid", "sock", "port")):
            raise RuntimeError("Refusing to adopt an unowned harness runtime")
        record = _read_record(path) if path.exists() else None
        if record and (record["cleanup"] == "retry" or time.time() - record["last_execution"] > _idle_timeout()):
            _finish_record(path, record)
            record = None
        record = record or {
            "version": 1, "home": str(home), "owner": owner, "name": name,
            "runtime": str(runtime), "cleanup": "active"}
        record["cleanup"] = "active"
        record.setdefault("last_execution", time.time())
        _write_record(path, record)
        _homes.add(str(home))
        from tools.browser_tool_lifecycle import _start_browser_cleanup_thread
        _start_browser_cleanup_thread()
        try:
            yield record
        finally:
            if record.pop("_executed", False):
                record["last_execution"] = time.time()
            try:
                generation = _transport(record, "inspect")
                if record.get("pid") and generation and any(
                        generation.get(key) != record.get(key) for key in ("pid", "start")):
                    raise RuntimeError("Harness generation changed; ownership retained")
                record.update(generation)
            except (RuntimeError, OSError, ValueError):
                record["cleanup"] = "retry"
                raise
            finally:
                _write_record(path, record)


def _mark_execution(record):
    record["last_execution"] = time.time()
    path = _state_dir() / f"{_record_id(record['home'], record['owner'], record['name'])}.json"
    _write_record(path, record)
    record["_executed"] = True  # process-local; never persisted as an in-flight lease


def _transport(record, operation):
    from tools.browser_use_cli import _base_subprocess_env
    env = _base_subprocess_env()
    _scope_env(env, record["home"], record["name"], record["runtime"])
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("browser_use_runtime.py"))],
                            input=json.dumps({"operation": operation, "pid": record.get("pid"),
                                              "start": record.get("start"), "kind": record.get("kind")}), env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=65, check=False)
    response = json.loads(result.stdout)
    if result.returncode or response.get("error"):
        raise RuntimeError(response.get("error", "Harness identity/shutdown probe failed"))
    return response


def _stop_record(record):
    if _transport(record, "stop").get("stopped") is not True:
        raise RuntimeError("Harness shutdown has not been confirmed")


def _finish_record(path, record):
    record["cleanup"] = "retry"
    _write_record(path, record)
    if not record.get("harness_stopped"):
        if not record.get("pid"):
            record.update(_transport(record, "inspect"))
            _write_record(path, record)
        _stop_record(record)
        record["harness_stopped"] = True
        _write_record(path, record)
    _release_backend(record)
    path.unlink()


def _release_backend(record):
    if not record.get("provider_id"):
        return
    from tools.browser_tool_cloud import _get_cloud_provider
    provider = _get_cloud_provider()
    if provider is None or provider.name != record["provider"]:
        raise RuntimeError("Owning browser provider is unavailable; retaining cleanup for retry")
    provider.close_session(record["provider_id"])


def _reuse_endpoint(record, env):
    endpoint = record.get("endpoint")
    if not endpoint or not record.get("pid"):
        return False
    generation = _transport(record, "inspect")
    if not generation:
        path = _state_dir() / f"{_record_id(record['home'], record['owner'], record['name'])}.json"
        _finish_record(path, record)  # fail closed if cloud cleanup or process death is unconfirmed
        identity = {key: record[key] for key in ("version", "home", "owner", "name", "runtime")}
        record.clear()
        record.update(identity, cleanup="active", last_execution=time.time())
        return False
    if generation.get("pid") != record["pid"] or generation.get("start") != record["start"]:
        raise RuntimeError("Retained harness generation is unavailable or changed; retry cleanup before reconnecting")
    from tools.browser_use_cli import _set_cdp_env, _BOT_DESKTOP_BROWSER_SENTINEL
    _set_cdp_env(env, endpoint)
    if record.get("desktop"):
        env[_BOT_DESKTOP_BROWSER_SENTINEL] = "1"
    return True


def _bind_backend(record, key, env):
    from tools.browser_tool import _active_sessions, _cleanup_lock
    from tools.browser_tool_cloud import _get_cloud_provider
    from tools.browser_use_cli import _BOT_DESKTOP_BROWSER_SENTINEL
    with _cleanup_lock:
        info = dict(_active_sessions.get(key) or {})
    record["endpoint"] = env.get("BU_CDP_WS") or env.get("BU_CDP_URL")
    record["desktop"] = bool(env.get(_BOT_DESKTOP_BROWSER_SENTINEL))
    record["backend_key"] = key
    if info:
        record["backend_name"] = info.get("session_name")
    if info.get("bb_session_id"):
        provider = _get_cloud_provider()
        if provider is None:
            raise RuntimeError("Browser provider disappeared during ownership registration")
        record.update(provider=provider.name, provider_id=info["bb_session_id"])
    path = _state_dir() / f"{_record_id(record['home'], record['owner'], record['name'])}.json"
    _write_record(path, record)


def _has_owned_backend(key):
    return any(_read_record(path).get("backend_key") == key for path in _state_dir().glob("*.json"))


def _stop_owned(backend_key=None):
    errors = []
    for index, path in enumerate(_state_dir().glob("*.json")):
        if index >= 4096:
            break
        try:
            if backend_key is not None and _read_record(path).get("backend_key") != backend_key:
                continue
            with _lease(path):
                record = _read_record(path)
                if backend_key is None or record.get("backend_key") == backend_key:
                    _finish_record(path, record)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("Harness ownership retained: " + "; ".join(errors[:5]))


def _idle_timeout():
    from tools.browser_use_cli import _read_browser_cfg
    from tools.browser_tool import DEFAULT_SESSION_INACTIVITY_TIMEOUT
    return max(30, int(_read_browser_cfg().get("inactivity_timeout", DEFAULT_SESSION_INACTIVITY_TIMEOUT)))


def _expire_profile():
    timeout = _idle_timeout()
    for index, path in enumerate(_state_dir().glob("*.json")):
        if index >= 4096:
            break
        try:
            with _lease(path):
                record = _read_record(path)
                if time.time() - record["last_execution"] <= timeout:
                    continue
                _finish_record(path, record)
        except TimeoutError:
            continue
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            _logger.warning("Harness idle cleanup retained %s: %s", path.name, exc)


def _backend_names():
    from tools.browser_tool_lifecycle import _session_owner_scope
    names = set()
    for home in tuple(_homes):
        with _session_owner_scope("", owner_home=home):
            for index, path in enumerate(_state_dir().glob("*.json")):
                if index >= 4096:
                    break
                try:
                    name = _read_record(path).get("backend_name")
                    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name):
                        names.add(name)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    _logger.warning("Ignoring invalid harness backend ownership %s: %s", path.name, exc)
    return names


def _detach_owner(owner):
    if not owner:
        return
    for index, path in enumerate(_state_dir().glob("*.json")):
        if index >= 4096:
            break
        try:
            with _lease(path):
                record = _read_record(path)
                if record["owner"] == owner:
                    record["cleanup"] = "detached"
                    _write_record(path, record)
        except TimeoutError:
            continue  # An executing call will finish and renew normally.


def _rehydrate(homes):
    """Bind the complete served set before the worker's first orphan pass."""
    from tools.browser_tool_lifecycle import _start_browser_cleanup_thread
    for home in homes:
        path = Path(home).resolve() / "cache" / "browser-use" / "owned"
        if not path.is_dir() or path.is_symlink() or not next(path.glob("*.json"), None):
            continue
        _homes.add(str(Path(home).resolve()))
    if _homes:
        _start_browser_cleanup_thread()


def _expire_owned():
    from tools.browser_tool_lifecycle import _session_owner_scope
    for home in tuple(_homes):
        with _session_owner_scope("", owner_home=home):
            _expire_profile()
