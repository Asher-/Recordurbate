import os
import plistlib
import subprocess
import sys
import time
import uuid

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


LAUNCHD_DEADLINE_SECONDS = 30


def install_service_copy(tmp_path, label, python_body):
    # The real service.sh in a project of its own, under a launchd label of its own
    bin_dir = tmp_path / "project" / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    with open(os.path.join(REPO, "service.sh")) as source_file:
        source = source_file.read()
    script = tmp_path / "project" / "service.sh"
    script.write_text(source.replace('LABEL="com.recordurbate.daemon"', 'LABEL="{}"'.format(label), 1))
    script.chmod(0o755)
    # Never let a copy reach the real com.recordurbate.daemon job
    assert 'LABEL="{}"'.format(label) in script.read_text()
    (bin_dir / "activate").write_text('PATH="{}:$PATH"\n'.format(bin_dir))
    python = bin_dir / "python"
    python.write_text("#!/bin/bash\n" + python_body + "\n")
    python.chmod(0o755)
    return script


@pytest.mark.skipif(sys.platform != "darwin", reason="service.sh manages launchd")
@pytest.mark.parametrize("python_body, exited", [("exit 3", "Exited:  3"), ("kill -TERM $$", "Exited:  signal 15")])
def test_enable_logs_outside_project_and_status_reports_last_exit(tmp_path, python_body, exited):
    label = "com.recordurbate.test-" + uuid.uuid4().hex
    script = install_service_copy(tmp_path, label, python_body)
    home = tmp_path / "home"
    log_dir = home / "Library" / "Logs" / "Recordurbate"
    env = dict(os.environ, HOME=str(home))
    try:
        subprocess.run([str(script), "enable"], env=env, check=True, capture_output=True)
        status = ""
        deadline = time.monotonic() + LAUNCHD_DEADLINE_SECONDS
        while exited + "\n" not in status and time.monotonic() < deadline:
            time.sleep(0.2)
            status = subprocess.run([str(script), "status"], env=env, capture_output=True, text=True).stdout

        with open(home / "Library" / "LaunchAgents" / (label + ".plist"), "rb") as plist_file:
            plist = plistlib.load(plist_file)
        assert plist["StandardOutPath"] == str(log_dir / "launchd.stdout.log")
        assert plist["StandardErrorPath"] == str(log_dir / "launchd.stderr.log")
        assert (log_dir / "launchd.stdout.log").exists()
        assert exited + "\n" in status
    finally:
        subprocess.run([str(script), "disable"], env=env, capture_output=True)


def poll_status(script, env, line):
    # launchd runs the job asynchronously, so wait for status to report its exit
    status = ""
    deadline = time.monotonic() + LAUNCHD_DEADLINE_SECONDS
    while line + "\n" not in status and time.monotonic() < deadline:
        time.sleep(0.2)
        status = subprocess.run([str(script), "status"], env=env, capture_output=True, text=True).stdout
    return status


@pytest.mark.skipif(sys.platform != "darwin", reason="service.sh manages launchd")
def test_enable_reloads_loaded_job_from_regenerated_plist(tmp_path):
    label = "com.recordurbate.test-" + uuid.uuid4().hex
    first = install_service_copy(tmp_path / "first", label, "exit 3")
    second = install_service_copy(tmp_path / "second", label, "exit 4")
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home))
    try:
        subprocess.run([str(first), "enable"], env=env, check=True, capture_output=True)
        assert "Exited:  3\n" in poll_status(first, env, "Exited:  3")

        # The job is loaded now, so this enable takes the reload route; only the
        # second project's job exits 4
        subprocess.run([str(second), "enable"], env=env, check=True, capture_output=True)
        status = poll_status(second, env, "Exited:  4")

        with open(home / "Library" / "LaunchAgents" / (label + ".plist"), "rb") as plist_file:
            plist = plistlib.load(plist_file)
        assert plist["WorkingDirectory"] == str(tmp_path / "second" / "project")
        assert "Exited:  4\n" in status
    finally:
        subprocess.run([str(second), "disable"], env=env, capture_output=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="service.sh manages launchd")
def test_enable_without_venv_python_installs_nothing(tmp_path):
    label = "com.recordurbate.test-" + uuid.uuid4().hex
    script = install_service_copy(tmp_path, label, "exit 3")
    venv_python = tmp_path / "project" / "venv" / "bin" / "python"
    venv_python.unlink()
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home))
    try:
        result = subprocess.run([str(script), "enable"], env=env, capture_output=True, text=True)

        assert result.returncode == 1
        assert result.stderr == "error: venv not found at {}\n".format(venv_python)
        assert not (home / "Library" / "LaunchAgents" / (label + ".plist")).exists()
        assert subprocess.run(["launchctl", "list", label], capture_output=True).returncode != 0
    finally:
        subprocess.run([str(script), "disable"], env=env, capture_output=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="service.sh manages launchd")
def test_status_of_job_never_enabled(tmp_path):
    label = "com.recordurbate.test-" + uuid.uuid4().hex
    script = install_service_copy(tmp_path, label, "exit 3")
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home))

    result = subprocess.run([str(script), "status"], env=env, capture_output=True, text=True)

    plist_path = home / "Library" / "LaunchAgents" / (label + ".plist")
    assert result.stdout == "Label:   {}\nPlist:   {}\nEnabled: no\nLoaded:  no\n".format(label, plist_path)


def running_pid_lines(status):
    return [line for line in status.splitlines() if line.startswith("PID:     ") and line[9:].isdigit()]


@pytest.mark.skipif(sys.platform != "darwin", reason="service.sh manages launchd")
def test_status_reports_pid_while_job_runs(tmp_path):
    label = "com.recordurbate.test-" + uuid.uuid4().hex
    script = install_service_copy(tmp_path, label, "exec sleep 60")
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    try:
        subprocess.run([str(script), "enable"], env=env, check=True, capture_output=True)
        status = ""
        deadline = time.monotonic() + LAUNCHD_DEADLINE_SECONDS
        while not running_pid_lines(status) and time.monotonic() < deadline:
            time.sleep(0.2)
            status = subprocess.run([str(script), "status"], env=env, capture_output=True, text=True).stdout

        assert running_pid_lines(status)
        assert "Exited:" not in status
    finally:
        subprocess.run([str(script), "disable"], env=env, capture_output=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="service.sh manages launchd")
def test_disable_unloads_job_and_removes_plist(tmp_path):
    label = "com.recordurbate.test-" + uuid.uuid4().hex
    script = install_service_copy(tmp_path, label, "exit 3")
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home))
    try:
        subprocess.run([str(script), "enable"], env=env, check=True, capture_output=True)
        assert "Exited:  3\n" in poll_status(script, env, "Exited:  3")

        loaded = subprocess.run([str(script), "disable"], env=env, capture_output=True, text=True)
        status = subprocess.run([str(script), "status"], env=env, capture_output=True, text=True).stdout
        # The job is unloaded now, so this disable takes the not-loaded route
        not_loaded = subprocess.run([str(script), "disable"], env=env, capture_output=True, text=True)

        assert loaded.stdout == "Disabled: {}\n".format(label)
        assert "Enabled: no\nLoaded:  no\n" in status
        assert not_loaded.returncode == 0
        assert not_loaded.stdout == "Disabled: {}\n".format(label)
        assert not (home / "Library" / "LaunchAgents" / (label + ".plist")).exists()
    finally:
        subprocess.run([str(script), "disable"], env=env, capture_output=True)


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
