"""Read-only client for the companion overlay app bridge feed
(ws://localhost:8765). Used for the current track name; the latest full message
is kept for later features (stages, standings).

Minimal RFC 6455 client on the standard library so the app has no extra deps.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
import threading
import time

log = logging.getLogger("aimod.companion")

PARSE_INTERVAL = 0.5
RECONNECT_DELAY = 3.0


class CompanionFeed:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host, self.port = host, port
        self.connected = False
        self.data: dict = {}
        self._raw: bytes | None = None
        self._last_parse = 0.0
        self.last_message = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self):
        if self.running:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="companion", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.connected = False

    @property
    def fresh(self) -> bool:
        return self.connected and time.monotonic() - self.last_message < 3.0

    def race_laps(self) -> tuple[int, int] | None:
        """(laps completed by the leader, race length) while at the track."""
        d = self.latest()
        lc, lt = d.get("lapsComplete"), d.get("lapsTotal")
        if not (self.fresh and d.get("atTrack") and isinstance(lc, int) and isinstance(lt, int) and lt > 0):
            return None
        return lc, lt

    @property
    def track(self) -> str:
        return str(self.latest().get("track") or "")

    def latest(self) -> dict:
        with self._lock:
            raw, self._raw = self._raw, None
        if raw is not None:
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict):
                    self.data.update(msg)
            except ValueError:
                pass
        return self.data

    # --------------------------------------------------------------- socket
    def _run(self, stop: threading.Event):
        while not stop.is_set():
            sock = None
            try:
                sock = self._connect()
                self.connected = True
                log.info("Connected to companion app feed on %s:%d", self.host, self.port)
                self._read_loop(sock, stop)
            except OSError:
                pass
            except Exception as e:
                log.debug("Companion app feed error: %s", e)
            finally:
                if self.connected:
                    log.info("Companion app feed disconnected")
                self.connected = False
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            stop.wait(RECONNECT_DELAY)

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise OSError("handshake closed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OSError("handshake rejected")
        self._buf = head.split(b"\r\n\r\n", 1)[1]
        sock.settimeout(10)
        return sock

    def _recv_exact(self, sock, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                raise OSError("closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send(self, sock, opcode: int, payload: bytes = b""):
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read_loop(self, sock, stop: threading.Event):
        message = b""
        while not stop.is_set():
            b0, b1 = self._recv_exact(sock, 2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(sock, 2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(sock, 8))[0]
            mask = self._recv_exact(sock, 4) if b1 & 0x80 else None
            payload = self._recv_exact(sock, n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                return
            if opcode == 0x9:
                self._send(sock, 0xA, payload)
                continue
            if opcode in (0x1, 0x2, 0x0):
                message += payload
                if fin:
                    now = time.monotonic()
                    self.last_message = now
                    if now - self._last_parse >= PARSE_INTERVAL:
                        self._last_parse = now
                        with self._lock:
                            self._raw = message
                    message = b""
