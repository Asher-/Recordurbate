import errno
import logging
import socket

import pytest

from ipc.server import SocketServer


class LoggingDaemon:
    # bind() needs nothing of its daemon but the logger
    logger = logging.getLogger("test_server")


def bound_server(port=0):
    server = SocketServer(daemon=LoggingDaemon(), hostname="127.0.0.1", port=port)
    server.bind()
    return server


def test_bind_of_held_port_reports_already_started(capsys):
    first = bound_server()
    try:
        second = SocketServer(daemon=LoggingDaemon(), hostname="127.0.0.1", port=first.ipc.getsockname()[1])
        with pytest.raises(SystemExit) as exited:
            second.bind()
    finally:
        first.ipc.close()

    assert exited.value.code == 1
    assert capsys.readouterr().out == "Already started.\n"


def test_bind_raises_other_bind_errors():
    # 192.0.2.1 is reserved for documentation, so no interface on this host carries it
    server = SocketServer(daemon=LoggingDaemon(), hostname="192.0.2.1", port=0)

    with pytest.raises(OSError) as raised:
        server.bind()

    assert raised.value.errno == errno.EADDRNOTAVAIL


def test_port_rebinds_while_a_served_connection_is_in_time_wait():
    # The daemon answers a stop request and closes that connection first, which
    # leaves it in TIME_WAIT on the daemon's port; the next daemon must still bind
    first = bound_server()
    port = first.ipc.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    served, _ = first.ipc.accept()
    served.close()
    client.close()
    first.ipc.close()

    second = bound_server(port)

    assert second.ipc.getsockname() == ("127.0.0.1", port)
    second.ipc.close()
