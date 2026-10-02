import os
import subprocess
import sys

import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(REPO, "venv")
CLI = os.path.join(REPO, "cli.py")
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


@pytest.mark.skipif(sys.platform != "darwin", reason="service.sh manages launchd")
def test_status_through_cli_matches_service_sh():
    via_cli = subprocess.run([sys.executable, CLI, "status"], capture_output=True, text=True, cwd=REPO)
    direct = subprocess.run([os.path.join(REPO, "service.sh"), "status"], capture_output=True, text=True, cwd=REPO)

    assert direct.stdout.startswith("Label:   com.recordurbate.daemon\n")
    assert via_cli.stdout == direct.stdout
    assert via_cli.returncode == direct.returncode


# usage

def test_usage_lists_service_commands(capsys):
    cli.usage()

    assert USAGE_LINE in capsys.readouterr().out


# venv re-exec

class Execed(Exception):
    pass


def run_reexec_block(monkeypatch, directory, prefix):
    # cli.py's re-exec block is the module code before its first third-party import
    with open(CLI) as source_file:
        source = source_file.read()
    block = compile(source[:source.index("from zeroconf")], CLI, "exec")
    calls = []

    def fake_execv(path, argv):
        calls.append([path, argv])
        raise Execed

    monkeypatch.setattr(os, "execv", fake_execv)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "argv", ["cli.py", "status"])
    try:
        exec(block, {"__file__": str(directory / "cli.py"), "__name__": "cli"})
    except Execed:
        pass
    return calls


def make_venv_python(directory):
    venv_python = directory / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    return str(venv_python)


def test_reexec_block_execs_venv_python_from_other_interpreter(monkeypatch, tmp_path):
    venv_python = make_venv_python(tmp_path)

    calls = run_reexec_block(monkeypatch, tmp_path, tmp_path / "other")

    assert calls == [[venv_python, [venv_python, str(tmp_path / "cli.py"), "status"]]]


def test_reexec_block_continues_under_venv_python(monkeypatch, tmp_path):
    make_venv_python(tmp_path)
    (tmp_path / "venv_link").symlink_to(tmp_path / "venv")

    assert run_reexec_block(monkeypatch, tmp_path, tmp_path / "venv_link") == []


def test_reexec_block_continues_without_venv(monkeypatch, tmp_path):
    assert run_reexec_block(monkeypatch, tmp_path, tmp_path / "other") == []


def test_other_interpreter_reexecs_under_project_venv():
    if not os.path.exists(os.path.join(VENV, "bin", "python")):
        pytest.skip("no project venv at " + VENV)
    base = sys._base_executable
    probe = subprocess.run([base, "-c", "import zeroconf"], capture_output=True)
    assert probe.returncode != 0, base + " imports zeroconf, so a passing run would not show the re-exec"

    result = subprocess.run([base, os.path.join(REPO, "cli.py"), "help"], capture_output=True, text=True, cwd=REPO)

    assert result.returncode == 0, result.stderr
    assert USAGE_LINE in result.stdout
