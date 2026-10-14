import types

import pytest

import ipc.discover_port
from ipc.discover_port import DiscoverPort


class FakeZeroconf:
    # Answers every lookup with the info it was built with and records each call
    def __init__(self, info):
        self.info = info
        self.lookups = []
        self.closed = False

    def get_service_info(self, type_, name, timeout):
        self.lookups.append((type_, name, timeout))
        return self.info

    def close(self):
        self.closed = True


def install_fake_zeroconf(monkeypatch, info):
    fake = FakeZeroconf(info)
    monkeypatch.setattr(ipc.discover_port, "Zeroconf", lambda: fake)
    return fake


def test_discover_returns_port_of_answered_lookup_and_closes(monkeypatch):
    fake = install_fake_zeroconf(monkeypatch, types.SimpleNamespace(port=4242))

    port = DiscoverPort("Recordurbate").discover(10)

    assert port == 4242
    assert fake.lookups == [("_recordurbate._tcp.local.", "Recordurbate._recordurbate._tcp.local.", 10000)]
    assert fake.closed


def test_discover_returns_none_for_unanswered_lookup_and_closes(monkeypatch):
    fake = install_fake_zeroconf(monkeypatch, None)

    port = DiscoverPort("Recordurbate").discover(10)

    assert port is None
    assert fake.closed


def test_discover_closes_zeroconf_when_lookup_raises(monkeypatch):
    fake = install_fake_zeroconf(monkeypatch, None)

    def failing_lookup(type_, name, timeout):
        raise OSError("No route to host")

    fake.get_service_info = failing_lookup

    with pytest.raises(OSError):
        DiscoverPort("Recordurbate").discover(10)

    assert fake.closed
