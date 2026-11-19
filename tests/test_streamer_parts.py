import os
import types

import pytest

import streamer
from streamer import Streamer


class FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, msg):
        self.messages.append(msg)

    def exception(self, msg):
        self.messages.append(msg)


def make_daemon(**overrides):
    config = {
        "youtube-dl_cmd": "yt-dlp",
        "youtube-dl_config": "youtube-dl.config",
    }
    config.update(overrides)
    launches = []
    return types.SimpleNamespace(
        logger=FakeLogger(),
        config=config,
        launches=launches,
        record_launch=lambda error=None: launches.append(error),
    )


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# wiring into Streamer.start

# What recorder.py logs once yt-dlp has resolved the room.
READY_LINE = "[recorder 2026-10-01 12:00:00] Recording alice to videos/alice/alice 2026-10-01 12_00.ts\n"

class FakeProc:
    pid = 4242
    ready_fd = None

    def __init__(self):
        self.returncode = None

    def release(self):
        # The recorder's end of the readiness pipe closes when it exits.
        if self.ready_fd is not None:
            os.close(self.ready_fd)
            self.ready_fd = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        self.release()
        return 0

    def terminate(self):
        self.returncode = -15
        self.release()

    def send_signal(self, sig):
        self.returncode = -2
        self.release()


def launch(factory, popen_kwargs, ready):
    # Holds the recorder's copy of the readiness pipe's write end, which
    # Streamer closes in the daemon right after Popen, as a real child would.
    proc = factory()
    proc.ready_fd = os.dup(popen_kwargs["pass_fds"][0])
    if ready:
        os.write(proc.ready_fd, b"ready\n")
        proc.release()
    elif proc.returncode is not None:
        proc.release()
    if hasattr(proc, "start"):
        proc.start()
    return proc


def run_start(streamer_obj):
    thread = streamer_obj.start()
    thread.join(timeout=10)
    assert not thread.is_alive()


# Streamer.start in isolation

class ExitedProc(FakeProc):
    def __init__(self):
        self.returncode = 0


@pytest.fixture
def start_harness(workdir, monkeypatch):
    calls = {"validated": 0}

    def fake_popen(*args, **kwargs):
        output = calls.get("output", READY_LINE)
        kwargs["stdout"].write(output)
        kwargs["stdout"].flush()
        return launch(calls.get("proc_factory", FakeProc), kwargs, ready="] Recording " in output)

    (workdir / "configs").mkdir()
    monkeypatch.setattr(streamer.subprocess, "Popen", fake_popen)
    return calls


def validation_returning(calls, result):
    def ensure_valid_stream(self, **kwargs):
        calls["validated"] += 1
        return result
    return ensure_valid_stream


def test_validated_recorder_is_waited_on(start_harness, monkeypatch):
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert any("Started to record alice." in m for m in daemon.logger.messages)


def test_start_does_not_record_launch_started_when_validation_fails(start_harness, monkeypatch):
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, False))
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert start_harness["validated"] == 1
    assert not any("Started to record" in m for m in daemon.logger.messages)


def test_start_does_not_validate_when_recorder_exits_before_ready(start_harness, monkeypatch):
    start_harness["output"] = ""
    start_harness["proc_factory"] = ExitedProc
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))

    run_start(Streamer(make_daemon(), "alice"))

    assert start_harness["validated"] == 0


def test_start_does_not_record_when_launch_fails(start_harness, monkeypatch):
    def failing_popen(*args, **kwargs):
        raise OSError("no yt-dlp")

    monkeypatch.setattr(streamer.subprocess, "Popen", failing_popen)
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert start_harness["validated"] == 0
    assert any("Failed to launch recorder for alice" in m for m in daemon.logger.messages)


def test_start_logs_and_stops_when_validation_raises(start_harness, monkeypatch):
    def exploding_validation(self, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(Streamer, "ensure_valid_stream", exploding_validation)
    daemon = make_daemon()
    s = Streamer(daemon, "alice")

    run_start(s)

    assert s.stream is None
    assert any("stream_thread error for alice" in m for m in daemon.logger.messages)


@pytest.mark.parametrize("state", [
    "Room is currently offline",
    "Room is currently in a private show",
    "Performer is currently away",
    "Room is password protected",
    "Hidden session in progress",
])
def test_room_state_counts_as_reaching_the_site(start_harness, state):
    start_harness["proc_factory"] = ExitedProc
    start_harness["output"] = "ERROR: [Chaturbate] alice: {}\n".format(state)
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [None]


@pytest.mark.parametrize("error", [
    "ERROR: [Chaturbate] alice: Unable to download webpage: curl: (60) unable to get local issuer certificate",
    "ERROR: [Chaturbate] alice: Unable to find stream URL",
    "ERROR: './chaturbate.com_cookies.txt' does not look like a Netscape format cookies file",
])
def test_any_other_error_is_reported_as_not_reaching_the_site(start_harness, error):
    start_harness["proc_factory"] = ExitedProc
    start_harness["output"] = error + "\n"
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [error]


def test_last_error_decides_when_ytdlp_warned_first(start_harness):
    # yt-dlp falls back to the webpage when its API call fails
    start_harness["proc_factory"] = ExitedProc
    start_harness["output"] = (
        "WARNING: [Chaturbate] alice: Unable to download JSON metadata: curl: (60)\n"
        "ERROR: [Chaturbate] alice: Room is currently offline\n"
    )
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [None]


def test_exit_without_an_error_is_not_reaching_the_site(start_harness):
    start_harness["proc_factory"] = ExitedProc
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [streamer.NO_ERROR]


def test_earlier_launches_in_the_capture_log_are_not_read(start_harness, workdir):
    (workdir / "configs/capture-alice.log").write_text("ERROR: [Chaturbate] alice: Room is currently offline\n")
    start_harness["proc_factory"] = ExitedProc
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [streamer.NO_ERROR]


def test_started_recording_counts_once_as_reaching_the_site(start_harness, monkeypatch):
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [None]


def test_failed_validation_is_not_reaching_the_site(start_harness, monkeypatch):
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, False))
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [streamer.NO_ERROR]


def test_stop_during_launch_reports_nothing(start_harness, monkeypatch):
    def stopped_during_validation(self, **kwargs):
        self.stop()
        return False

    monkeypatch.setattr(Streamer, "ensure_valid_stream", stopped_during_validation)
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == []


def test_real_process_output_is_read_back_from_the_capture_log(workdir):
    # The real recorder.py runs the fake yt-dlp, whose stderr must reach the
    # capture log through the recorder.
    error = "ERROR: [Chaturbate] alice: Unable to download webpage: curl: (6) Could not resolve host"
    (workdir / "configs").mkdir()
    (workdir / "configs/capture-alice.log").write_text("ERROR: [Chaturbate] alice: Room is currently offline\n")
    script = workdir / "yt-dlp"
    script.write_text("#!/bin/sh\necho '{}' >&2\nexit 1\n".format(error))
    script.chmod(0o755)
    (workdir / "configs/config.json").write_text(
        '{{"youtube-dl_cmd": "{}", "youtube-dl_config": "configs/youtube-dl.config"}}'.format(script))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    (workdir / "recorder.py").symlink_to(os.path.join(root, "recorder.py"))
    daemon = make_daemon(**{"youtube-dl_cmd": str(script)})

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [error]


def on_readiness_wait(monkeypatch, action):
    # Runs action as Streamer enters its readiness wait, then waits for real,
    # so the event provably lands inside the wait.
    real_select = streamer.select.select
    def select_after_action(*args):
        action()
        return real_select(*args)
    monkeypatch.setattr(streamer.select, "select", select_after_action)


def slow_lookup(output, exit_code, monkeypatch):
    # A recorder whose yt-dlp lookup finishes once Streamer is waiting for it,
    # while Streamer is in its readiness wait: it writes output to the capture log, then
    # signals ready when exit_code is None, or exits with exit_code.
    class SlowLookup(FakeProc):
        def start(self):
            on_readiness_wait(monkeypatch, self.finish)

        def finish(self):
            with open("configs/capture-alice.log", "a") as f:
                f.write(output)
            if exit_code is None:
                os.write(self.ready_fd, b"ready\n")
            else:
                self.returncode = exit_code
            self.release()
    return SlowLookup


def test_offline_room_reported_during_the_readiness_wait_is_not_started(start_harness, monkeypatch):
    start_harness["output"] = ""
    start_harness["proc_factory"] = slow_lookup("ERROR: [Chaturbate] alice: Room is currently offline\n", 1, monkeypatch)
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [None]
    assert start_harness["validated"] == 0
    assert not any("Started to record" in m for m in daemon.logger.messages)


def test_readiness_signalled_during_the_wait_starts_recording(start_harness, monkeypatch):
    start_harness["output"] = ""
    start_harness["proc_factory"] = slow_lookup(READY_LINE, None, monkeypatch)
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [None]
    assert any("Started to record alice." in m for m in daemon.logger.messages)


def test_recorder_that_never_logs_recording_is_not_started(start_harness, monkeypatch):
    start_harness["output"] = ""
    monkeypatch.setattr(streamer, "READY_TIMEOUT", 1)
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))
    daemon = make_daemon()
    s = Streamer(daemon, "alice")

    run_start(s)

    assert daemon.launches == [streamer.NO_ERROR]
    assert start_harness["validated"] == 0
    assert s.stream is None
    assert not any("Started to record" in m for m in daemon.logger.messages)


def test_stop_during_the_readiness_wait_reports_nothing(start_harness, monkeypatch):
    start_harness["output"] = ""
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))
    daemon = make_daemon()
    s = Streamer(daemon, "alice")
    on_readiness_wait(monkeypatch, s.stop)

    run_start(s)

    assert daemon.launches == []
    assert start_harness["validated"] == 0
    assert s.stream is None


# Streamer.adopt: a recorder recorded by an earlier daemon

class FakeAdopted:
    waited = 0
    terminated = 0

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        FakeAdopted.waited += 1
        self.returncode = -1
        return -1

    def terminate(self):
        FakeAdopted.terminated += 1
        self.returncode = -15


def s_terminated(fake):
    return fake.terminated == 1


@pytest.fixture
def adopt_harness(workdir, monkeypatch):
    (workdir / "configs").mkdir()
    record = workdir / "configs/pid-alice"
    record.write_text("4242\n")
    FakeAdopted.waited = 0
    FakeAdopted.terminated = 0
    monkeypatch.setattr(streamer, "AdoptedProcess", FakeAdopted)
    return record


def run_adopt(streamer_obj, pid):
    thread = streamer_obj.adopt(pid)
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_valid_adopted_recorder_is_waited_on(adopt_harness, monkeypatch):
    monkeypatch.setattr(Streamer, "ensure_valid_stream", lambda self, **kwargs: True)
    daemon = make_daemon()
    s = Streamer(daemon, "alice")

    run_adopt(s, 4242)

    assert FakeAdopted.waited == 1
    assert any("Adopted stream for alice validated." in m for m in daemon.logger.messages)
    assert daemon.launches == []
    assert s.stream is None
    assert not s.started


def test_adopted_recorder_that_fails_validation_is_terminated(adopt_harness, monkeypatch):
    monkeypatch.setattr(Streamer, "ensure_valid_stream", lambda self, **kwargs: False)
    daemon = make_daemon()
    s = Streamer(daemon, "alice")

    run_adopt(s, 4242)

    assert s_terminated(FakeAdopted)
    assert any("failed validation, will restart" in m for m in daemon.logger.messages)
    assert not any("validated." in m for m in daemon.logger.messages)
    assert s.stream is None


def test_adopt_error_is_logged_and_streamer_cleared(adopt_harness, monkeypatch):
    def exploding_validation(self, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(Streamer, "ensure_valid_stream", exploding_validation)
    daemon = make_daemon()
    s = Streamer(daemon, "alice")

    run_adopt(s, 4242)

    assert any("adopted_thread error for alice" in m for m in daemon.logger.messages)
    assert s.stream is None
