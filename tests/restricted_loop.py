"""Optional asyncio test policy for Windows sandboxes without socketpair.

Only mocked async I/O is exercised here. A timer polls thread-safe callbacks
instead of a loopback wakeup socket. Real socket integration tests must still
run with the normal policy on an unrestricted development machine.
"""
import asyncio
import os


def install():
    if os.name != "nt":
        return

    class PollingLoop(asyncio.ProactorEventLoop):
        def _make_self_pipe(self):
            class NoSignalSocket:
                def fileno(self):
                    return -1
            self._ssock = None
            self._csock = NoSignalSocket()
            self._test_heartbeat = None

        def _write_to_self(self):
            pass

        def _loop_self_reading(self, future=None):
            if self._test_heartbeat is not None:
                self._test_heartbeat.cancel()
            if not self.is_closed():
                self._test_heartbeat = self.call_later(0.01, self._loop_self_reading)

        def _close_self_pipe(self):
            if self._test_heartbeat is not None:
                self._test_heartbeat.cancel()

    class Policy(asyncio.WindowsProactorEventLoopPolicy):
        _loop_factory = PollingLoop

    asyncio.set_event_loop_policy(Policy())
