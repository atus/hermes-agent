"""Plugin generations resolve for their installation host, not other OSes."""
import os
from pathlib import Path
import shutil
import sys

import pytest

from pm.environment import PythonEnvironment
from pm.workspace import lock_and_sync


@pytest.mark.platforms("linux", "macos", "windows")
def test_plugin_ignores_foreign_os(tmp_path):
    core = tmp_path / "core"
    plugin = tmp_path / "plugin"
    core.mkdir()
    plugin.mkdir()
    (core / "pyproject.toml").write_text(
        '[project]\nname="host-core"\nversion="1"\n'
        'requires-python=">=3.14"\n'
        'dependencies=["unavailable==1; sys_platform == \'android\'"]\n'
        '[tool.uv]\npackage=false\nno-index=true\n'
        'environments=["python_version >= \'3.14\'"]\n',
        encoding="utf-8",
    )
    (plugin / "pyproject.toml").write_text(
        '[project]\nname="host-plugin"\nversion="1"\n'
        'requires-python=">=3.14"\n[tool.uv]\npackage=false\n',
        encoding="utf-8",
    )
    (plugin / "plugin.yaml").write_text("name: host-plugin\n", encoding="utf-8")
    uv = shutil.which("uv")
    assert uv is not None
    environment = PythonEnvironment(
        uv=Path(uv), python=Path(sys.executable), destination=tmp_path / "venv",
        cache=tmp_path / "cache", env=dict(os.environ), offline=True,
    )
    before = (core / "pyproject.toml").read_bytes()
    generated = tmp_path / "workspace"
    lock_and_sync(
        [plugin], [], root=generated, source=core,
        seed_lock=None, environment=environment,
    )
    environment.check()
    assert (generated / "uv.lock").is_file()
    assert (core / "pyproject.toml").read_bytes() == before
