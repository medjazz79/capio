"""Capability checks for tests that need a real local TCP connection."""
from functools import lru_cache
import socket
import unittest


@lru_cache(maxsize=1)
def loopback_available():
    try:
        with socket.socket() as listener, socket.socket() as client:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client.settimeout(0.2)
            return client.connect_ex(listener.getsockname()) == 0
    except OSError:
        return False


requires_loopback = unittest.skipUnless(
    loopback_available(), "The execution environment blocks local TCP connections")
