import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

import daemon
from daemon import Daemon
from ipc.ipc_config import IpcConfig

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bare_daemon():
    # Daemon() loads configs/config.json and builds streamers; binding needs neither
    instance = Daemon.__new__(Daemon)
    instance.logger = logging.getLogger("test_daemon")
    instance.pid = None
    return instance


def hold_port(monkeypatch):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind((IpcConfig.host, 0))
    holder.listen(1)
    monkeypatch.setattr(IpcConfig, "port", holder.getsockname()[1])
    return holder


def test_daemonize_reports_held_port_before_forking(monkeypatch, capsys):
    holder = hold_port(monkeypatch)
    forks = []

    def fake_fork():
        forks.append(True)
        raise AssertionError("forked while another daemon held the port")

    monkeypatch.setattr(daemon.os, "fork", fake_fork)
    try:
        with pytest.raises(SystemExit) as exited:
            bare_daemon().daemonize()
    finally:
        holder.close()

    assert exited.value.code == 1
    assert capsys.readouterr().out == "Already started.\n"
    assert forks == []


def test_start_foreground_reports_held_port_before_running(monkeypatch, capsys):
    holder = hold_port(monkeypatch)
    runs = []
    monkeypatch.setattr(Daemon, "run", lambda self: runs.append(True))
    try:
        with pytest.raises(SystemExit) as exited:
            bare_daemon().start_foreground()
    finally:
        holder.close()

    assert exited.value.code == 1
    assert capsys.readouterr().out == "Already started.\n"
    assert runs == []


def test_run_loop_starts_no_further_streamers_once_stop_is_requested(monkeypatch):
    monkeypatch.setattr(daemon, "find_running_ytdlp", lambda: {})
    instance = bare_daemon()
    instance.pid = os.getpid()
    instance.config = {"auto_reload_config": False, "rate_limit_time": 0}
    instance.streamers_lock = threading.Lock()
    started = []

    class StopRequestingStreamer:
        stream = None

        def __init__(self, name):
            self.name = name

        def start(self):
            started.append(self.name)
            # stop and handoff both clear pid; this one arrives mid-pass
            instance.pid = None

        def stop(self):
            pass

    instance.streamers = {name: StopRequestingStreamer(name) for name in ["first", "second", "third"]}

    thread = instance.run()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert started == ["first"]


def counting_daemon(*streamers):
    instance = bare_daemon()
    instance.launches_lock = threading.Lock()
    instance.streamers = dict.fromkeys(streamers)
    return instance


def test_warns_once_when_a_pass_worth_of_launches_fail(caplog):
    instance = counting_daemon("first", "second")
    caplog.set_level(logging.DEBUG, logger="test_daemon")

    instance.record_launch("ERROR: one")
    assert caplog.records == []

    instance.record_launch("ERROR: two")
    instance.record_launch("ERROR: three")

    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert "ERROR: two" in caplog.records[0].getMessage()


def test_launch_reaching_the_site_restarts_the_count(caplog):
    instance = counting_daemon("first", "second")
    caplog.set_level(logging.DEBUG, logger="test_daemon")

    instance.record_launch("ERROR: one")
    instance.record_launch()
    instance.record_launch("ERROR: two")

    assert caplog.records == []


def test_logs_recovery_once_and_warns_again_on_the_next_outage(caplog):
    instance = counting_daemon("only")
    caplog.set_level(logging.DEBUG, logger="test_daemon")

    instance.record_launch("ERROR: first outage")
    instance.record_launch()
    instance.record_launch()
    instance.record_launch("ERROR: second outage")

    assert [record.levelno for record in caplog.records] == [logging.WARNING, logging.INFO, logging.WARNING]
    assert "ERROR: second outage" in caplog.records[2].getMessage()


# Daemonizes a bare Daemon on a free port, then reports from the daemon process
# whether fds 0-2 are /dev/null and stays alive, as a running daemon would
DAEMONIZE_SCRIPT = """
import json, logging, os, sys, time
from daemon import Daemon
from ipc.ipc_config import IpcConfig

IpcConfig.port = 0
instance = Daemon.__new__(Daemon)
instance.logger = logging.getLogger("test_daemon")
instance.pid = None
instance.daemonize()
devnull = os.stat(os.devnull)
report = {
    "pid": os.getpid(),
    "devnull": [(lambda s: (s.st_dev, s.st_ino) == (devnull.st_dev, devnull.st_ino))(os.fstat(fd)) for fd in (0, 1, 2)],
}
with open(sys.argv[1] + ".tmp", "w") as report_file:
    json.dump(report, report_file)
os.rename(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(30)
"""

DAEMON_REPORT_DEADLINE_SECONDS = 10


def test_daemonize_releases_the_callers_stdio(tmp_path):
    report_path = tmp_path / "report.json"
    caller = subprocess.Popen(
        [sys.executable, "-c", DAEMONIZE_SCRIPT, str(report_path)],
        cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    report = None
    try:
        # Returns only once every process holding the caller's pipes has let go
        caller.communicate(timeout=DAEMON_REPORT_DEADLINE_SECONDS)
        deadline = time.monotonic() + DAEMON_REPORT_DEADLINE_SECONDS
        while not report_path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        report = json.loads(report_path.read_text())

        os.kill(report["pid"], 0)  # the daemon is still running
        assert report["devnull"] == [True, True, True]
    finally:
        caller.kill()
        if report is None and report_path.exists():
            report = json.loads(report_path.read_text())
        if report is not None:
            try:
                os.kill(report["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
