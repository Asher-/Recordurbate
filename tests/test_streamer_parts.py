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


def make_daemon():
    config = {
        "youtube-dl_cmd": "yt-dlp",
        "youtube-dl_config": "youtube-dl.config",
        "process_poll_wait_time": 0,
    }
    return types.SimpleNamespace(logger=FakeLogger(), config=config)


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
