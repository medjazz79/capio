"""Loopback HTTP streams for progressively synthesized speech audio."""
from __future__ import annotations

import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STREAM_QUEUE_MAX_BYTES = 2 * 1024 * 1024


class _AudioStream:
    def __init__(self):
        self.queue: queue.Queue[bytes | None] = queue.Queue()
        self._closed = threading.Event()
        self._state_changed = threading.Condition()
        self._finished = False
        self._queued_bytes = 0
        self._reader_claimed = False

    def claim_reader(self) -> bool:
        """A stream queue can feed exactly one HTTP response."""
        with self._state_changed:
            if self._reader_claimed or self._closed.is_set():
                return False
            self._reader_claimed = True
            return True

    def write(self, audio: bytes) -> bool:
        for offset in range(0, len(audio), STREAM_QUEUE_MAX_BYTES):
            chunk = audio[offset:offset + STREAM_QUEUE_MAX_BYTES]
            with self._state_changed:
                while (
                    self._queued_bytes + len(chunk) > STREAM_QUEUE_MAX_BYTES
                    and not self._closed.is_set()
                    and not self._finished
                ):
                    self._state_changed.wait()
                if self._closed.is_set() or self._finished:
                    return False
                self._queued_bytes += len(chunk)
                self.queue.put_nowait(chunk)
        return True

    def get(self) -> bytes | None:
        return self.queue.get()

    def mark_sent(self, audio_bytes: int) -> None:
        with self._state_changed:
            self._queued_bytes = max(0, self._queued_bytes - audio_bytes)
            self._state_changed.notify_all()

    def finish(self) -> None:
        with self._state_changed:
            if self._finished or self._closed.is_set():
                return
            self._finished = True
            self.queue.put_nowait(None)
            self._state_changed.notify_all()

    def close(self) -> None:
        with self._state_changed:
            if self._closed.is_set():
                return
            self._closed.set()
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self._queued_bytes = 0
            self.queue.put_nowait(None)
            self._state_changed.notify_all()


class AudioStreamServer:
    """Serve Edge TTS audio chunks to QMediaPlayer before synthesis completes."""

    def __init__(self):
        self._streams: dict[str, _AudioStream] = {}
        self._lock = threading.Lock()
        self._closed = False
        ready = threading.Event()
        owner = self

        class Server(ThreadingHTTPServer):
            def service_actions(self) -> None:
                ready.set()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                token = self.path.partition("?")[0].lstrip("/")
                with owner._lock:
                    stream = owner._streams.get(token)
                if stream is None:
                    self.send_error(404)
                    return
                if not stream.claim_reader():
                    self.send_error(409, "Audio stream already has a reader")
                    return

                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = stream.get()
                        if chunk is None:
                            break
                        try:
                            self.wfile.write(
                                f"{len(chunk):X}\r\n".encode("ascii")
                                + chunk + b"\r\n"
                            )
                            self.wfile.flush()
                        finally:
                            stream.mark_sent(len(chunk))
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    # The player can close the connection when speech is stopped.
                    return
                finally:
                    owner._remove_stream(token, stream)

            def log_message(self, _format: str, *args: object) -> None:
                del _format, args

        self._server = Server(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05),
            name="speech-audio-stream",
            daemon=True,
        )
        self._thread.start()
        if not ready.wait(timeout=2):
            self._server.server_close()
            raise RuntimeError("Local speech audio stream server failed to start.")

    def create_stream(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(24)
        with self._lock:
            if self._closed:
                raise RuntimeError("Local speech audio stream server is closed.")
            self._streams[token] = _AudioStream()
        return token, f"http://127.0.0.1:{self._server.server_port}/{token}"

    def write(self, token: str, audio: bytes) -> bool:
        if not audio:
            return True
        with self._lock:
            if self._closed:
                return False
            stream = self._streams.get(token)
        return stream.write(audio) if stream is not None else False

    def finish(self, token: str) -> None:
        with self._lock:
            if self._closed:
                return
            stream = self._streams.get(token)
        if stream is not None:
            stream.finish()

    def _remove_stream(self, token: str, stream: _AudioStream) -> None:
        with self._lock:
            if self._streams.get(token) is stream:
                del self._streams[token]
        stream.close()

    def abort_all(self) -> None:
        """Invalidate reads immediately while retaining the listening server."""
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            stream.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            stream.close()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
