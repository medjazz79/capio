"""Requests-compatible transport with cooperative socket cancellation.

The scope belongs to a worker. Cancelling it shuts down its sockets, including
while waiting for response headers or the next streaming frame. No replacement
request is started just to make the UI responsive.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import io
import socket
import threading
import subprocess
import time

import requests as _requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool


class RequestCancelled(RuntimeError):
    pass


_CANCEL_POLL_SECONDS = 0.1


class _CancellableSocketReader(io.RawIOBase):
    def __init__(self, sock, scope, timeout):
        super().__init__()
        self._sock = sock
        self._scope = scope
        self._timeout = timeout

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("I/O operation on closed socket reader")
        deadline = (
            None if self._timeout is None
            else time.monotonic() + self._timeout
        )
        while True:
            self._scope.check()
            if deadline is None:
                timeout = _CANCEL_POLL_SECONDS
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("timed out")
                timeout = min(_CANCEL_POLL_SECONDS, remaining)
            try:
                self._sock.settimeout(timeout)
                size = self._sock.recv_into(buffer)
                self._scope.check()
                return size
            except socket.timeout:
                if deadline is not None and time.monotonic() >= deadline:
                    raise
            except OSError:
                self._scope.check()
                raise


class _CancellableSocket:
    def __init__(self, sock, scope):
        self._sock = sock
        self._scope = scope

    def makefile(self, mode="r", buffering=None):
        if mode != "rb":
            return self._sock.makefile(mode, buffering)
        timeout = self._sock.gettimeout()
        raw = _CancellableSocketReader(self._sock, self._scope, timeout)
        buffer_size = (
            io.DEFAULT_BUFFER_SIZE
            if buffering in (None, -1)
            else buffering
        )
        return io.BufferedReader(raw, buffer_size=buffer_size)

    def __getattr__(self, name):
        return getattr(self._sock, name)


_SCOPE = ContextVar("capio_http_cancellation", default=None)


class _Cancellation:
    def __init__(self, cancelled):
        self.cancelled = cancelled
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.connections = []
        self.sockets = []
        self.processes = []
        self.thread = threading.Thread(target=self._watch, daemon=True)

    def check(self):
        if self.cancelled():
            raise RequestCancelled("Request cancelled")

    def register(self, connection):
        with self.lock:
            self.connections.append(connection)
        self.check()

    def _watch(self):
        while not self.stopped.wait(0.025):
            if not self.cancelled():
                continue
            with self.lock:
                connections = list(self.connections)
                sockets = list(self.sockets)
                processes = list(self.processes)
            for process in processes:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
            for sock in sockets + [getattr(connection, "sock", None) for connection in connections]:
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        sock.close()
                    except OSError:
                        pass
            # Keep watching: a socket may still be completing its TCP connect.


@contextmanager
def cancellation_scope(cancelled):
    scope = _Cancellation(cancelled)
    token = _SCOPE.set(scope)
    scope.thread.start()
    try:
        scope.check()
        yield
    finally:
        scope.stopped.set()
        scope.thread.join(timeout=0.2)
        _SCOPE.reset(token)


def _adapter(scope):
    class ConnectionMixin:
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            scope.register(self)

        def connect(self):
            scope.check()
            super().connect()
            try:
                scope.check()
            except RequestCancelled:
                self.close()
                raise

        def getresponse(self):
            if self.sock is not None:
                self.sock = _CancellableSocket(self.sock, scope)
            return super().getresponse()

    class CancelHTTPConnection(ConnectionMixin, HTTPConnection):
        pass

    class CancelHTTPSConnection(ConnectionMixin, HTTPSConnection):
        pass

    class HTTPPool(HTTPConnectionPool):
        ConnectionCls = CancelHTTPConnection

    class HTTPSPool(HTTPSConnectionPool):
        ConnectionCls = CancelHTTPSConnection

    class Adapter(HTTPAdapter):
        @staticmethod
        def _configure(manager):
            manager.pool_classes_by_scheme = {"http": HTTPPool, "https": HTTPSPool}

        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self._configure(self.poolmanager)

        def proxy_manager_for(self, *args, **kwargs):
            manager = super().proxy_manager_for(*args, **kwargs)
            self._configure(manager)
            return manager

    return Adapter(max_retries=0)


def request(method, url, **kwargs):
    scope = _SCOPE.get()
    if scope is None:
        return _requests.request(method, url, **kwargs)
    scope.check()
    # Bound DNS/connect failures; retain the caller's read timeout for generation.
    timeout = kwargs.get("timeout", 20)
    if isinstance(timeout, (int, float)):
        kwargs["timeout"] = (min(10, timeout), timeout)
    with _requests.Session() as session:
        adapter = _adapter(scope)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        try:
            response = session.request(method, url, **kwargs)
        except Exception:
            scope.check()
            raise
        original_iter_content = response.iter_content

        def iter_content(*args, **content_kwargs):
            try:
                yield from original_iter_content(*args, **content_kwargs)
            except Exception:
                scope.check()
                raise

        response.iter_content = iter_content
        # HTTP/1.0 can detach the connection's socket after headers. Retain the
        # response's socket as well so a stalled body remains cancellable.
        raw_socket = getattr(getattr(getattr(getattr(response.raw, "_fp", None), "fp", None), "raw", None), "_sock", None)
        if raw_socket is not None:
            with scope.lock:
                scope.sockets.append(raw_socket)
        try:
            scope.check()
        except RequestCancelled:
            response.close()
            raise
        return response


def get(url, **kwargs):
    if _SCOPE.get() is None:
        return _requests.get(url, **kwargs)
    return request("GET", url, **kwargs)


def register_process(process):
    scope = _SCOPE.get()
    if scope is not None:
        with scope.lock:
            scope.processes.append(process)
        try:
            scope.check()
        except RequestCancelled:
            if process.poll() is None:
                process.kill()
            raise


def run_process(command, *, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """The CLI route has the same cancellation lifetime as HTTP providers."""
    scope = _SCOPE.get()
    if scope is None:
        return subprocess.run(command, input=input, capture_output=capture_output,
                              timeout=timeout, check=check, **kwargs)
    scope.check()
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    with subprocess.Popen(command, **kwargs) as process:
        try:
            register_process(process)
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        scope.check()
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def post(url, **kwargs):
    if _SCOPE.get() is None:
        return _requests.post(url, **kwargs)
    return request("POST", url, **kwargs)


def __getattr__(name):
    return getattr(_requests, name)
