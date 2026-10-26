import logging
import socket

import pytest

import daemon
from daemon import Daemon
from ipc.ipc_config import IpcConfig


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
