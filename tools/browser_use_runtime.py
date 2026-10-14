"""Isolated harness IPC driver: never discovers Chrome or signals a process.

Run as a script so browser_harness's import-time IPC directory is scoped to
one owned record, without mutating the gateway's environment or module cache.
"""
import json
import os
import sys
import time

import psutil


def _process(pid):
    if type(pid) is not int or not 0 < pid < (1 << 31):
        raise RuntimeError("Harness returned an invalid process identity")
    proc = psutil.Process(pid)
    argv = proc.cmdline()
    if argv[1:3] != ["-m", "browser_harness.daemon"]:
        raise RuntimeError("Harness PID is not the daemon module")
    env = proc.environ()
    for key in ("BH_RUNTIME_DIR", "BU_NAME"):
        if env.get(key) != os.environ[key]:
            raise RuntimeError("Harness process is not bound to this owned runtime")
    return proc


def _generation(proc):
    return {"pid": proc.pid, "start": proc.create_time()}


def _alive(pid, start):
    try:
        proc = psutil.Process(pid)
        return proc.create_time() == start and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _run(request):
    from browser_harness import _ipc as ipc

    name = os.environ["BU_NAME"]
    pid = ipc.identify(name, timeout=2)
    if pid is None:
        if request["operation"] == "inspect":
            return {}
        if request.get("kind") in {"cloud", "unknown"}:
            raise RuntimeError("Owned cloud cleanup authority is unavailable; billing cleanup is unconfirmed")
        expected = request.get("pid")
        if expected and _alive(expected, request.get("start")):
            raise RuntimeError("Owned harness is alive but IPC shutdown is unavailable")
        if ipc.pid_path(name).exists() and not expected:
            raise RuntimeError("Unidentified harness startup retained for retry")
        return {"stopped": True}
    proc = _process(pid)
    generation = _generation(proc)
    if request["operation"] == "inspect":
        connection, token = ipc.connect(name, timeout=2)
        try:
            reply = ipc.request(connection, token, {"meta": "ping"})
        finally:
            connection.close()
        kind = reply.get("browser_kind")
        return {**generation, "kind": kind if kind in {"cloud", "cdp", "local"} else "unknown"}
    if generation != {"pid": request.get("pid"), "start": request.get("start")}:
        raise RuntimeError("Harness generation changed; ownership retained")
    # The daemon's supported shutdown stops cloud billing before acknowledging,
    # and closes only its dedicated target on shared CDP, never Browser.close.
    connection, token = ipc.connect(name, timeout=50)
    try:
        if ipc.identify(name, timeout=2) != pid or _generation(proc) != generation:
            raise RuntimeError("Harness generation changed during shutdown")
        reply = ipc.request(connection, token, {"meta": "shutdown"})
    finally:
        connection.close()
    if not isinstance(reply, dict) or reply.get("ok") is not True or reply.get("error"):
        raise RuntimeError(reply.get("error", "Harness did not confirm clean shutdown") if isinstance(reply, dict)
                           else "Harness did not confirm clean shutdown")
    deadline = time.monotonic() + 5
    while _alive(pid, generation["start"]):
        if time.monotonic() >= deadline:
            raise RuntimeError("Harness acknowledged shutdown but has not exited; retained for retry")
        time.sleep(.05)
    ipc.cleanup_endpoint(name)
    return {"stopped": True}


if __name__ == "__main__":
    try:
        print(json.dumps(_run(json.loads(sys.stdin.read(16384)))))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, psutil.Error) as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
