import os
import subprocess
import sys

import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(REPO, "venv")
USAGE_LINE = "Recordurbate [enable | disable | status]"

# Importing cli under any interpreter other than the project venv re-execs this
# process as cli.py, which would end the test run with cli.py's output.
if os.path.exists(os.path.join(VENV, "bin", "python")) and os.path.realpath(sys.prefix) != os.path.realpath(VENV):
    pytest.skip("run the suite with " + os.path.join(VENV, "bin", "python"), allow_module_level=True)

import cli


# service

@pytest.mark.parametrize("command", ["enable", "disable", "status"])
def test_service_command_runs_service_sh(command, monkeypatch):
    calls = []

    def fake_call(argv):
        calls.append(argv)
        return 3

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    monkeypatch.setattr(cli.sys, "argv", ["cli.py", command])

    with pytest.raises(SystemExit) as exit_info:
        cli.argument_map[command]()

    assert calls == [[os.path.join(REPO, "service.sh"), command]]
    assert exit_info.value.code == 3


# usage

def test_usage_lists_service_commands(capsys):
    cli.usage()

    assert USAGE_LINE in capsys.readouterr().out


# venv re-exec

def test_other_interpreter_reexecs_under_project_venv():
    if not os.path.exists(os.path.join(VENV, "bin", "python")):
        pytest.skip("no project venv at " + VENV)
    base = sys._base_executable
    probe = subprocess.run([base, "-c", "import zeroconf"], capture_output=True)
    assert probe.returncode != 0, base + " imports zeroconf, so a passing run would not show the re-exec"

    result = subprocess.run([base, os.path.join(REPO, "cli.py"), "help"], capture_output=True, text=True, cwd=REPO)

    assert result.returncode == 0, result.stderr
    assert USAGE_LINE in result.stdout
