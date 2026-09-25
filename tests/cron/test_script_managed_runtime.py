"""Cron Python scripts must boot the selected dependencies, not bare store Python."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix", "windows")
def test_script_managed_runtime(tmp_path, monkeypatch):
    from cron import scheduler_script
    from hermes_cli import _launchers
    from pm.environments import install_state_dir, site_packages
    from tools.environments.local import build_subprocess_env

    source = Path(scheduler_script.__file__).resolve().parents[1]
    repo = tmp_path / "source"
    # Exercise the real bootstrap without source-update recovery touching the checkout.
    for relative in (
        "hermes_bootstrap.py", "hermes_constants.py", "hermes_cli/__init__.py",
        "hermes_cli/_early_recovery.py", "hermes_cli/_parser.py",
        "hermes_cli/venv_sync.py", "hermes_cli/steward.py",
        "hermes_cli/runtime_state.py", "pm/environments.py", "pm/filesystem.py",
        "pm/paths.py",
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    monkeypatch.setattr(scheduler_script, "__file__", str(repo / "cron/scheduler_script.py"))
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))
    interpreter = Path(sys._base_executable).resolve()
    (store / "facts.json").write_text(json.dumps({"packages": {"python": {
        "entry": str(interpreter.parent if os.name == "nt" else interpreter.parents[1]),
    }}}), encoding="utf-8")
    python = _launchers.resolve_store_python(repo)
    assert python is not None
    monkeypatch.setattr(sys, "executable", str(python))

    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "sibling_helper.py").write_text("VALUE = 'sibling'\n", encoding="utf-8")
    script = scripts / "probe 'café.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "import cron_selected_probe, cron_editable_probe, sibling_helper\n"
        "from hermes_cli.runtime_state import leases_held\n"
        "from pm.environments import selected_venv\n"
        f"generation = selected_venv(Path({str(repo)!r})).parent\n"
        "print(json.dumps({'selected': cron_selected_probe.VALUE,\n"
        "    'editable': cron_editable_probe.VALUE, 'sibling': sibling_helper.VALUE,\n"
        "    'argv': sys.argv, 'file': __file__, 'name': __name__,\n"
        "    'cwd': os.getcwd(), 'secret': os.environ.get('OPENAI_API_KEY'),\n"
        "    'leased': leases_held(generation)}))\n"
        "sys.exit(7 if sys.argv[1:] else 0)\n",
        encoding="utf-8",
    )
    editable = tmp_path / "editable"
    editable.mkdir()
    (editable / "cron_editable_probe.py").write_text("VALUE = 'editable'\n", encoding="utf-8")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-script")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = build_subprocess_env(strip_launch_profile=True)
    bare = subprocess.run(
        [str(python), "-I", "-c", "import cron_selected_probe"],
        env=env, cwd=workdir, capture_output=True, text=True, timeout=30,
    )
    assert bare.returncode != 0 and "No module named 'cron_selected_probe'" in bare.stderr

    command = None
    for value in ("first", "replacement"):
        selected = install_state_dir(repo) / "environments" / value / "venv"
        site = site_packages(selected)
        site.mkdir(parents=True)
        (selected / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
        (selected.parent / ".lease-managed").touch()
        (site / "cron_selected_probe.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
        (site / "editable.pth").write_text(str(editable) + "\n", encoding="utf-8")
        (install_state_dir(repo) / "facts.json").write_text(
            json.dumps({"packages": {"venv": {"environment": str(selected)}}}), encoding="utf-8",
        )
        args = [] if command is None else ["spaces and 'quotes'", ""]
        if command is None:
            success, output = scheduler_script._run_job_script(script.name, workdir=str(workdir))
            assert success, output
            command, overlay, error = scheduler_script._script_argv(script)
            assert error is None
            env.update(overlay)
        else:
            # A prepared command must select/lease at child start, not freeze the old generation.
            result = subprocess.run(
                [*command, *args], env=env, cwd=workdir,
                capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 7, result.stdout + result.stderr
            output = result.stdout
        assert json.loads(output) == {
            "selected": value, "editable": "editable", "sibling": "sibling",
            "argv": [str(script), *args], "file": str(script), "name": "__main__",
            "cwd": str(workdir), "secret": None, "leased": True,
        }
