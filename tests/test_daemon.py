import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading

import pytest

import daemon
from daemon import Daemon
from ipc.ipc_config import IpcConfig
from streamer import Streamer
from fifo_wait import read_fifo

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
    monkeypatch.setattr(daemon, "recorded_recorder", lambda name: None)
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


def test_stop_ends_the_run_loops_wait_at_once(monkeypatch):
    monkeypatch.setattr(daemon, "recorded_recorder", lambda name: None)
    instance = bare_daemon()
    instance.config = {"auto_reload_config": False, "rate_limit_time": 0}
    # the streamer holds the pass until the wait is being recorded, so the
    # loop reaches its minute wait only through the recorded wait
    waits = run_with_recorded_waits(instance, GatedStreamer(fail=False))

    assert waits == [60]


@pytest.mark.parametrize("method", ["stop", "handoff"])
def test_stop_and_handoff_clear_pid_and_set_wake(caplog, method):
    instance = bare_daemon()
    instance.pid = os.getpid()
    instance.wake = threading.Event()

    # Returns rather than exiting; the process ends when start's join returns
    with caplog.at_level(logging.DEBUG):
        assert getattr(instance, method)() is None

    assert instance.pid is None
    assert instance.wake.is_set()
    assert instance._handoff == (method == "handoff")
    if method == "stop":
        # stop runs in the signal handler, which writes no log
        assert caplog.records == []


def run_with_recorded_waits(instance, streamer):
    # Runs the loop with one streamer and records each timeout the loop waits
    # on wake for. streamer.start is called once the recording is in place.
    instance.pid = os.getpid()
    instance.streamers_lock = threading.Lock()
    instance.streamers = {"room": streamer}
    waits = []
    waiting = threading.Event()
    thread = instance.run()
    real_wait = instance.wake.wait

    def record_wait(timeout):
        waits.append(timeout)
        waiting.set()
        return real_wait(timeout)

    instance.wake.wait = record_wait
    streamer.go.set()
    assert waiting.wait(timeout=5)
    instance.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    return waits


class GatedStreamer:
    stream = None

    def __init__(self, fail):
        self.fail = fail
        self.go = threading.Event()
        self.starts = 0

    def start(self):
        assert self.go.wait(timeout=5)
        self.starts += 1
        if self.fail:
            raise RuntimeError("launch failed")

    def stop(self):
        pass


def test_run_loop_waits_for_wake_after_an_error(monkeypatch):
    monkeypatch.setattr(daemon, "recorded_recorder", lambda name: None)
    instance = bare_daemon()
    instance.config = {"auto_reload_config": False, "rate_limit_time": 0}
    streamer = GatedStreamer(fail=True)

    waits = run_with_recorded_waits(instance, streamer)

    # the failed pass waits out the minute on wake; stop ends that wait
    assert waits == [60]
    assert streamer.starts == 1


def test_stop_ends_the_rate_limit_pause(monkeypatch):
    monkeypatch.setattr(daemon, "recorded_recorder", lambda name: None)
    instance = bare_daemon()
    instance.config = {"auto_reload_config": False, "rate_limit_time": 45}
    streamer = GatedStreamer(fail=False)

    waits = run_with_recorded_waits(instance, streamer)

    # the pause after a launch is a wait on wake, so stop ends it at once
    assert waits == [45]
    assert streamer.starts == 1


def test_shutdown_stops_streamers_together(monkeypatch):
    monkeypatch.setattr(daemon, "recorded_recorder", lambda name: None)
    instance = bare_daemon()
    instance.pid = os.getpid()
    instance.config = {"auto_reload_config": False, "rate_limit_time": 0}
    instance.streamers_lock = threading.Lock()
    # each stop returns only once both are stopping at the same time
    both_stopping = threading.Barrier(2, timeout=5)
    stopped = []

    class BarrierStreamer:
        stream = object()  # recording, so the loop launches nothing

        def __init__(self, name):
            self.name = name

        def start(self):
            raise AssertionError("a recording streamer is not started")

        def stop(self):
            both_stopping.wait()
            stopped.append(self.name)

    instance.streamers = {name: BarrierStreamer(name) for name in ["first", "second"]}

    thread = instance.run()
    instance.stop()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert sorted(stopped) == ["first", "second"]


@pytest.mark.parametrize("method", ["stop", "handoff"])
def test_run_with_a_real_recorder_ends_on_stop_and_handoff(monkeypatch, tmp_path, method):
    # Daemon.run launches recorder.py through Streamer. fake_ytdlp.py resolves
    # the room to a server that accepts connections and never answers, so the
    # recorder runs until the daemon ends it.
    unanswering = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    unanswering.bind(("127.0.0.1", 0))
    unanswering.listen(16)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "config.json").write_text(json.dumps({
        "youtube-dl_cmd": "{} {}".format(sys.executable, os.path.join(REPO, "tests", "fake_ytdlp.py")),
        "youtube-dl_config": "configs/unused.config",
    }))
    (tmp_path / "recorder.py").symlink_to(os.path.join(REPO, "recorder.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_HLS_BASE", "http://127.0.0.1:{}".format(unanswering.getsockname()[1]))
    # adopt_existing runs for real: configs/ holds no record, so nothing is adopted
    instance = counting_daemon()
    instance.pid = os.getpid()
    instance.config = {"auto_reload_config": False, "rate_limit_time": 0}
    instance.streamers_lock = threading.Lock()
    streamer = Streamer(instance, "room")
    instance.streamers = {"room": streamer}
    launched = threading.Event()
    real_record_launch = instance.record_launch

    def record_and_report(error=None):
        real_record_launch(error)
        launched.set()

    instance.record_launch = record_and_report

    thread = instance.run()
    recorder = None
    try:
        assert launched.wait(timeout=30)
        recorder = streamer.stream
        assert recorder is not None and recorder.poll() is None

        getattr(instance, method)()
        thread.join(timeout=30)

        assert not thread.is_alive()
        if method == "stop":
            assert recorder.poll() is not None
        else:
            assert recorder.poll() is None
    finally:
        if recorder is not None and recorder.poll() is None:
            os.killpg(recorder.pid, signal.SIGKILL)
            recorder.wait()
        unanswering.close()


class AdoptingStreamer:
    def __init__(self, stream=None):
        self.stream = stream
        self.adopted = []

    def adopt(self, pid):
        self.adopted.append(pid)


def test_adopt_existing_adopts_each_recorded_recorder_once(monkeypatch):
    records = {"alice": 41, "bob": None, "carol": 43}
    monkeypatch.setattr(daemon, "recorded_recorder", lambda name: records[name])
    instance = bare_daemon()
    instance.streamers_lock = threading.Lock()
    # carol's streamer already has a stream, so her record is left alone
    instance.streamers = {"alice": AdoptingStreamer(), "bob": AdoptingStreamer(), "carol": AdoptingStreamer(stream=object())}

    instance.adopt_existing()

    assert instance.streamers["alice"].adopted == [41]
    assert instance.streamers["bob"].adopted == []
    assert instance.streamers["carol"].adopted == []


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
with open(sys.argv[1], "w") as report_file:  # a FIFO the test is reading
    json.dump(report, report_file)
time.sleep(30)
"""

DAEMON_REPORT_DEADLINE_SECONDS = 10


def test_daemonize_releases_the_callers_stdio(tmp_path):
    report_path = tmp_path / "report.json"
    os.mkfifo(report_path)
    caller = subprocess.Popen(
        [sys.executable, "-c", DAEMONIZE_SCRIPT, str(report_path)],
        cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    report = None
    try:
        # Returns only once every process holding the caller's pipes has let go
        caller.communicate(timeout=DAEMON_REPORT_DEADLINE_SECONDS)
        # Blocks until the daemon opens the FIFO, writes its report and closes it
        report = json.loads(read_fifo(report_path, DAEMON_REPORT_DEADLINE_SECONDS))

        os.kill(report["pid"], 0)  # the daemon is still running
        assert report["devnull"] == [True, True, True]
    finally:
        caller.kill()
        if report is not None:
            try:
                os.kill(report["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


RESTART_SCRIPT = """
import logging, os, sys, threading
from daemon import Daemon
instance = Daemon.__new__(Daemon)
instance.logger = logging.getLogger("restart")
instance.pid = os.getpid()
instance.config = {"auto_reload_config": False, "rate_limit_time": 0}
instance.streamers_lock = threading.Lock()
instance.streamers = {}
# the command restart runs: a program that reports the PID it runs as
instance.restart_argv = [sys.executable, "-c",
    "import os, sys; open(sys.argv[1], 'w').write(str(os.getpid()))", sys.argv[1]]
thread = instance.run()
getattr(instance, sys.argv[2])(client_response=None)
thread.join()
"""


@pytest.mark.parametrize("method", ["restart", "stop"])
def test_restart_reexecutes_in_the_same_process_and_stop_does_not(tmp_path, method):
    marker = tmp_path / "executed"
    process = subprocess.Popen(
        [sys.executable, "-c", RESTART_SCRIPT, str(marker), method],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    _, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    if method == "restart":
        # the run loop replaced the daemon's own process with restart_argv
        assert marker.read_text() == str(process.pid)
    else:
        assert not marker.exists()


def test_start_restarts_with_cli_start_whatever_started_it(monkeypatch):
    instance = bare_daemon()
    monkeypatch.setattr(daemon.sys, "argv", ["/project/cli.py", "upgrade"])
    monkeypatch.setattr(instance, "daemonize", lambda: None)
    ended = threading.Thread(target=lambda: None)
    ended.start()
    monkeypatch.setattr(instance, "run", lambda: ended)

    instance.start()
    assert instance.restart_argv == [sys.executable, "/project/cli.py", "start"]

    monkeypatch.setattr(instance, "start_foreground", lambda: None)
    instance.start(foreground=True)
    assert instance.restart_argv == [sys.executable, "/project/cli.py", "start", "--foreground"]
