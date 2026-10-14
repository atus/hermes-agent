"""Exercise pinned harness target ownership without starting or connecting Chrome."""
import subprocess
import sys

from tools import browser_use_lifecycle as life


def test_pinned_harness_closes_only_its_one_dedicated_tab(monkeypatch):
    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: None)
    env = {}
    probe = '''
import asyncio
from browser_harness import daemon as module
class CDP:
    def __init__(self):
        self.pages = [{"targetId": "user-existing", "type": "page", "url": "https://example.com"}]
        self.created = []
        self.closed = []
    async def send_raw(self, method, params=None, **kwargs):
        assert method != "Browser.close"
        if method == "Target.getTargets": return {"targetInfos": self.pages}
        if method == "Target.createTarget":
            self.created.append("harness-owned")
            self.pages.append({"targetId": "harness-owned", "type": "page", "url": "about:blank"})
            return {"targetId": "harness-owned"}
        if method == "Target.attachToTarget": return {"sessionId": "owned-session"}
        if method == "Target.closeTarget": self.closed.append(params["targetId"])
        return {}
async def parked(*args): await asyncio.Event().wait()
async def run():
    module.log = lambda *args: None
    module.ipc.serve = parked
    daemon = module.Daemon()
    daemon.cdp = CDP()
    daemon.stop = asyncio.Event()
    await daemon.attach_first_page(enable_domains=False)
    await daemon.attach_first_page(enable_domains=False)
    assert daemon.cdp.created == ["harness-owned"]
    daemon.target_id = "user-existing"  # switching does not transfer ownership
    daemon.stop.set()
    await module.serve(daemon)
    assert daemon.cdp.closed == ["harness-owned"]
    print("one owned tab created and closed; existing tab preserved")
asyncio.run(run())
'''
    with life._execution(env, "tab-contract", "research"):
        result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                                text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "existing tab preserved" in result.stdout
