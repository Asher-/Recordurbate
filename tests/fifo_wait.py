"""Bounded FIFO opens for tests.

Opening a FIFO blocks until its other end is opened, so a test whose peer
never arrives would hang instead of failing. These helpers do the blocking
open on a thread and fail the test once the timeout passes.
"""
import os
import threading


def open_fifo(path, mode, timeout):
    # Returns path opened in mode ("r" or "w") once the peer has opened its end.
    return _on_thread(path, mode, timeout, lambda: open(path, mode))


def read_fifo(path, timeout):
    # Returns everything written to path, once its writer has opened and closed it.
    def read():
        with open(path) as fifo:
            return fifo.read()
    return _on_thread(path, "r", timeout, read)


def _on_thread(path, mode, timeout, action):
    outcome = {}

    def run():
        outcome["value"] = action()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        # open the other end ourselves so the blocked open returns
        flags = os.O_WRONLY if mode == "r" else os.O_RDONLY
        try:
            os.close(os.open(path, flags | os.O_NONBLOCK))
        except OSError:
            pass
        thread.join(timeout)
        value = outcome.get("value")
        if hasattr(value, "close"):
            value.close()
        raise AssertionError("no peer opened {} within {}s".format(path, timeout))
    return outcome["value"]
