"""Test package marker and settings isolation.

The explicit package prevents test modules from resolving against another
checkout. It also redirects test saves away from the user's real settings
file, including for targeted `python -m unittest tests.test_module` runs.
"""
import os
import pathlib
import tempfile
import ipaddress
import socket

import config.settings as _settings

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SETTINGS_ENV = "HARMONY_TEST_SETTINGS_PATH"

if SETTINGS_ENV not in os.environ:
    os.environ[SETTINGS_ENV] = str(
        pathlib.Path(tempfile.mkdtemp(prefix="harmony-tests-settings-"))
        / "settings.json"
    )

_settings.SETTINGS_PATH = pathlib.Path(os.environ[SETTINGS_ENV])

if os.environ.get("CAPIO_TEST_POLL_LOOP") == "1":
    from .restricted_loop import install
    install()


# These are offline tests. An accidentally unmocked provider must fail locally,
# never consume a user's quota or key. Loopback servers remain available for
# transport/audio integration tests.
def _local_host(host):
    if host in ("localhost", b"localhost", None):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if not getattr(socket, "_capio_offline_tests", False):
    _connect = socket.socket.connect
    _getaddrinfo = socket.getaddrinfo

    def _offline_connect(sock, address):
        if isinstance(address, tuple) and not _local_host(address[0]):
            raise OSError("External network access is disabled in Capio tests")
        return _connect(sock, address)

    def _offline_getaddrinfo(host, *args, **kwargs):
        if not _local_host(host):
            raise OSError("External network access is disabled in Capio tests")
        return _getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = _offline_connect
    socket.getaddrinfo = _offline_getaddrinfo
    socket._capio_offline_tests = True
