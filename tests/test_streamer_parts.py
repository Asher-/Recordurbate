import os
import time
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
        "process_poll_wait_time": 0,
    }
    config.update(overrides)
    launches = []
    return types.SimpleNamespace(
        logger=FakeLogger(),
        config=config,
        launches=launches,
        record_launch=lambda error=None: launches.append(error),
    )


def write(path, mtime, data=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# finalize_prior_parts

def test_renames_part_written_before_launch(workdir):
    before = time.time()
    part = write(workdir / "videos/alice/alice 2026-09-12 16_27.mp4.part", before - 600)
    other = write(workdir / "videos/alice/alice 2026-09-11 10_00.mp4", before - 600)

    Streamer(make_daemon(), "alice").finalize_prior_parts(before)

    assert not part.exists()
    assert (workdir / "videos/alice/alice 2026-09-12 16_27.mp4").read_bytes() == b"data"
    assert other.exists()


def test_keeps_part_written_at_or_after_launch(workdir):
    before = time.time()
    at_launch = write(workdir / "videos/alice/a.mp4.part", before)
    after_launch = write(workdir / "videos/alice/b.mp4.part", before + 5)

    Streamer(make_daemon(), "alice").finalize_prior_parts(before)

    assert at_launch.exists()
    assert after_launch.exists()
    assert not (workdir / "videos/alice/a.mp4").exists()
    assert not (workdir / "videos/alice/b.mp4").exists()


def test_existing_target_leaves_both_files_untouched(workdir):
    before = time.time()
    part = write(workdir / "videos/alice/a.mp4.part", before - 600, b"part")
    target = write(workdir / "videos/alice/a.mp4", before - 600, b"target")
    daemon = make_daemon()

    Streamer(daemon, "alice").finalize_prior_parts(before)

    assert part.read_bytes() == b"part"
    assert target.read_bytes() == b"target"
    assert any("already exists" in m for m in daemon.logger.messages)


def test_other_streamer_directory_untouched(workdir):
    before = time.time()
    (workdir / "videos/alice").mkdir(parents=True)
    bob_part = write(workdir / "videos/bob/b.mp4.part", before - 600)

    Streamer(make_daemon(), "alice").finalize_prior_parts(before)

    assert bob_part.exists()
    assert not (workdir / "videos/bob/b.mp4").exists()


def test_missing_directory_does_not_raise(workdir):
    daemon = make_daemon()

    Streamer(daemon, "alice").finalize_prior_parts(time.time())

    assert any("Failed to list" in m for m in daemon.logger.messages)


def test_rename_failure_is_logged_and_part_kept(workdir, monkeypatch):
    before = time.time()
    part = write(workdir / "videos/alice/a.mp4.part", before - 600)
    daemon = make_daemon()

    def failing_rename(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr(streamer.os, "rename", failing_rename)
    Streamer(daemon, "alice").finalize_prior_parts(before)

    assert part.exists()
    assert not (workdir / "videos/alice/a.mp4").exists()
    assert any("Failed to rename" in m for m in daemon.logger.messages)


# wiring into Streamer.start

class FakeProc:
    pid = 4242

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15

    def send_signal(self, sig):
        self.returncode = -2


@pytest.fixture
def fake_launch(workdir, monkeypatch):
    # The fake yt-dlp launch writes the new recording's .part and stamps it
    # with the moment of launch, as ffmpeg would once it starts writing.
    new_part = workdir / "videos/alice/alice 2026-10-01 12_00.mp4.part"

    def fake_popen(*args, **kwargs):
        write(new_part, time.time())
        return FakeProc()

    (workdir / "configs").mkdir()
    monkeypatch.setattr(streamer.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(Streamer, "wait_with_watchdog", lambda self, proc: None)
    monkeypatch.setattr(Streamer, "cleanup_ffmpeg", lambda self: None)
    return new_part


def run_start(streamer_obj):
    thread = streamer_obj.start()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_validated_start_finalizes_prior_parts_only(workdir, fake_launch, monkeypatch):
    old_part = write(workdir / "videos/alice/alice 2026-09-30 22_15.mp4.part", time.time() - 600)
    monkeypatch.setattr(Streamer, "ensure_valid_stream", lambda self, **kwargs: True)

    run_start(Streamer(make_daemon(), "alice"))

    assert not old_part.exists()
    assert (workdir / "videos/alice/alice 2026-09-30 22_15.mp4").exists()
    assert fake_launch.exists()


def test_failed_validation_renames_nothing(workdir, fake_launch, monkeypatch):
    old_part = write(workdir / "videos/alice/alice 2026-09-30 22_15.mp4.part", time.time() - 600)
    monkeypatch.setattr(Streamer, "ensure_valid_stream", lambda self, **kwargs: False)

    run_start(Streamer(make_daemon(), "alice"))

    assert old_part.exists()
    assert fake_launch.exists()


# Streamer.start in isolation: finalize_prior_parts is faked so only start's own
# routes are under test.

class ExitedProc(FakeProc):
    def __init__(self):
        self.returncode = 0


@pytest.fixture
def start_harness(workdir, monkeypatch):
    calls = {"popen_at": None, "finalize": [], "validated": 0}

    def fake_popen(*args, **kwargs):
        calls["popen_at"] = time.time()
        kwargs["stdout"].write(calls.get("output", ""))
        kwargs["stdout"].flush()
        return calls.get("proc_factory", FakeProc)()

    def fake_finalize(self, before):
        calls["finalize"].append(before)

    (workdir / "configs").mkdir()
    monkeypatch.setattr(streamer.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(Streamer, "wait_with_watchdog", lambda self, proc: None)
    monkeypatch.setattr(Streamer, "cleanup_ffmpeg", lambda self: None)
    monkeypatch.setattr(Streamer, "finalize_prior_parts", fake_finalize)
    return calls


def validation_returning(calls, result):
    def ensure_valid_stream(self, **kwargs):
        calls["validated"] += 1
        return result
    return ensure_valid_stream


def test_start_finalizes_with_launch_time_taken_before_popen(start_harness, monkeypatch):
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))
    daemon = make_daemon(process_poll_wait_time=1)

    run_start(Streamer(daemon, "alice"))

    assert start_harness["finalize"] == [start_harness["finalize"][0]]
    assert start_harness["finalize"][0] <= start_harness["popen_at"]
    assert any("Started to record alice." in m for m in daemon.logger.messages)


def test_start_does_not_finalize_when_validation_fails(start_harness, monkeypatch):
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, False))

    run_start(Streamer(make_daemon(), "alice"))

    assert start_harness["validated"] == 1
    assert start_harness["finalize"] == []


def test_start_does_not_validate_when_ytdlp_exits_during_poll(start_harness, monkeypatch):
    start_harness["proc_factory"] = ExitedProc
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))

    run_start(Streamer(make_daemon(process_poll_wait_time=None), "alice"))

    assert start_harness["validated"] == 0
    assert start_harness["finalize"] == []


def test_start_does_not_finalize_when_launch_fails(start_harness, monkeypatch):
    def failing_popen(*args, **kwargs):
        raise OSError("no yt-dlp")

    monkeypatch.setattr(streamer.subprocess, "Popen", failing_popen)
    monkeypatch.setattr(Streamer, "ensure_valid_stream", validation_returning(start_harness, True))
    daemon = make_daemon()

    run_start(Streamer(daemon, "alice"))

    assert start_harness["validated"] == 0
    assert start_harness["finalize"] == []
    assert any("Failed to launch yt-dlp for alice" in m for m in daemon.logger.messages)


def test_start_logs_and_stops_when_validation_raises(start_harness, monkeypatch):
    def exploding_validation(self, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(Streamer, "ensure_valid_stream", exploding_validation)
    daemon = make_daemon()
    s = Streamer(daemon, "alice")

    run_start(s)

    assert start_harness["finalize"] == []
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
    error = "ERROR: [Chaturbate] alice: Unable to download webpage: curl: (6) Could not resolve host"
    (workdir / "configs").mkdir()
    (workdir / "configs/capture-alice.log").write_text("ERROR: [Chaturbate] alice: Room is currently offline\n")
    script = workdir / "yt-dlp"
    script.write_text("#!/bin/sh\necho '{}' >&2\nexit 1\n".format(error))
    script.chmod(0o755)
    daemon = make_daemon(**{"youtube-dl_cmd": str(script), "process_poll_wait_time": 5})

    run_start(Streamer(daemon, "alice"))

    assert daemon.launches == [error]
