"""Repository-owned helper adapter, loaded inside the CLI's Python process.

This module uses only the standard library. It does not modify the installed
harness, and it is a resource-ownership guard, not an arbitrary-Python sandbox.
"""
import json
import os
from pathlib import Path


class _TabScope:
    def __init__(self, helpers, record):
        self._helpers = helpers
        self._send_cdp = helpers.cdp
        self._owner_state = os.environ.get('_HERMES_BU_OWNER_STATE')
        self._generation = os.environ.get('_HERMES_BU_GENERATION')
        self._record = Path(record)
        self._epoch = self._raw_cdp("Target.getTargetInfo")["targetInfo"]["targetId"]
        self._targets = []
        self._current = None
        self._session = None
        if self._record.exists():
            saved = json.loads(self._record.read_text(encoding='utf-8'))
            if saved.get("epoch") == self._epoch:
                self._targets = saved["targets"]
                self._current = saved.get("current")
        self.ensure_real_tab()

    def _assert_open(self):
        if not self._owner_state:
            return
        path = Path(self._owner_state)
        if not path.exists() or path.read_text(encoding='utf-8') != self._generation:
            raise RuntimeError('Browser owner generation is closed')

    def _raw_cdp(self, method, **params):
        self._assert_open()
        return self._send_cdp(method, **params)

    def _save(self):
        self._assert_open()
        self._write_record()

    def _write_record(self):
        data = {"epoch": self._epoch, "targets": self._targets, "current": self._current}
        temporary = self._record.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding='utf-8')
        temporary.replace(self._record)

    def _record_created(self, target):
        self._targets.append(target)
        # Cleanup waits on this CLI's lane lock. Journal the response even if
        # closure raced creation; never discard the only exact target ID.
        self._write_record()
        try:
            self._assert_open()
        except RuntimeError:
            try:
                result = self._send_cdp('Target.closeTarget', targetId=target)
                if result.get('success'):
                    self._targets.remove(target)
                    self._write_record()
            except Exception:
                pass  # The journal remains available for cleanup/retry.
            raise

    def _create_tab(self):
        target = self._raw_cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self._record_created(target)
        self._attach(target)
        return target

    def _attach(self, target):
        self._session = self._raw_cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        self._helpers._send({"meta": "set_session", "session_id": self._session, "target_id": target})
        self._current = target
        self._save()

    def current_tab(self):
        return self.ensure_real_tab()

    def ensure_real_tab(self):
        tabs = self._raw_cdp("Target.getTargets")["targetInfos"]
        # Vanished parents still establish ownership of live descendants.
        self._discover_popups(tabs)
        live = {tab["targetId"] for tab in tabs}
        self._targets = [target for target in self._targets if target in live]
        if self._current not in self._targets:
            self._current = next(iter(self._targets), None)
            self._session = None
        if self._current is None:
            self._create_tab()
        elif self._session is None:
            self._attach(self._current)
        self._save()
        info = self._raw_cdp("Target.getTargetInfo", targetId=self._current)["targetInfo"]
        return {**info, "target_id": self._current}

    def _require_owned(self, target):
        if isinstance(target, dict):
            target = target.get("targetId") or target.get("target_id")
        if target not in self._targets:
            raise ValueError(f"Tab {target!r} is not owned by this lane")
        return target

    def new_tab(self, url="about:blank"):
        target = self._create_tab()
        if url != "about:blank":
            self._helpers.goto_url(url)
        return target

    def _discover_popups(self, tabs):
        while True:
            children = [tab['targetId'] for tab in tabs if tab.get('type') == 'page'
                        and tab.get('openerId') in self._targets and tab['targetId'] not in self._targets]
            if not children:
                return
            self._targets.extend(children)
            self._save()

    def list_tabs(self, include_chrome=True):
        targets = self._raw_cdp("Target.getTargets")["targetInfos"]
        self._discover_popups(targets)
        return [
            {**tab, "target_id": tab["targetId"]} for tab in targets
            if tab["targetId"] in self._targets and tab["type"] == "page"
            and (include_chrome or not tab.get("url", "").startswith(("chrome:", "about:", "devtools:")))
        ]

    def switch_tab(self, target, activate=False):
        target = self._require_owned(target)
        self._attach(target)
        if activate:
            self._raw_cdp("Target.activateTarget", targetId=target)
        return self._session

    def close_tab(self, target=None):
        target = self._require_owned(self._current if target is None else target)
        # Preserve live descendants before closing their ownership anchor.
        self._discover_popups(self._raw_cdp("Target.getTargets")["targetInfos"])
        result = self._raw_cdp("Target.closeTarget", targetId=target)
        if not result.get('success'):
            return result
        self._targets.remove(target)
        if self._current == target:
            self._current = None
            self._session = None
        self._save()
        return result

    def cdp(self, method, session_id=None, **params):
        if method in {'Browser.close', 'Target.disposeBrowserContext'}:
            raise ValueError('Cannot perform browser-wide teardown from an owned lane')
        if method == "Target.createTarget":
            result = self._raw_cdp(method, **params)
            self._record_created(result['targetId'])
            return result
        if method == "Target.closeTarget":
            return self.close_tab(params.get("targetId"))
        # Explicit sessions/targets support OOPIF and other advanced CDP use.
        # Tab helper ownership is a lifecycle policy, not a CDP sandbox.
        if session_id is None and not method.startswith("Target."):
            self.ensure_real_tab()
        return self._raw_cdp(method, session_id=session_id or self._session, **params)


def install_scope(helpers, record):
    """Bind the existing harness helper surface to one owned set of targets."""
    scope = _TabScope(helpers, record)
    for name in ("cdp", "current_tab", "new_tab", "list_tabs", "switch_tab", "close_tab", "ensure_real_tab"):
        setattr(helpers, name, getattr(scope, name))
    return scope
