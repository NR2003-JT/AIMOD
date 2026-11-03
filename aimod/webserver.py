"""HTTP endpoint for the pit board widget and the AIMOD Live dashboard.

  GET /          -> AIMOD Live dashboard (running order, pit log, pit board)
  GET /monitor.json, /pitlog.json -> the dashboard's live data

  GET /pits     -> live pit board JSON (CORS open, so the page can be served by
                   the companion app bridge on :8766 and still read it)
  GET /overlay  -> the widget page itself (for OBS when the companion app isn't running)
  GET /asset/<path> -> number art / headshots from the companion app's folder (read-only, PNG only)

  Scanner on a phone (this PC only; a phone app on the network passes them on):
  GET /radio.json?listen=1   -> the last few scanner calls (asking with listen=1 = a phone is listening)
  GET /radio/<id>.wav        -> one call as it sounds on the PC
  GET /radio/phone_only?on=1 -> the PC stays quiet while a phone listens (0 = both play)
  GET /crew.json             -> crew chief on a phone (beta): every car as its crew chief sees it
  GET /crew/claim?car=&name= -> a phone takes an AI car (returns its token); /crew/ping?token= keeps it,
      /crew/release?token= gives it back, /crew/call?token=&caution=&pit=1&service=4|R|L|F&fuel=1-5 its caution call,
      /crew/plan?token=&lap=&service=&fuel= its next green-flag stop (&clear=1 leaves it to AIMOD),
      /crew/adjust?token=&wedge=&bar=&air=&tape= (steps, -3..3) its changes at the next stop (&clear=1: AIMOD's)
"""
from __future__ import annotations

import json
import logging
import mimetypes
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .dashboard_html import DASHBOARD_HTML
from .widget_html import WIDGET_HTML

log = logging.getLogger("aimod.web")
DEFAULT_PORT = 8767


class BoardServer:
    def __init__(self, port: int = DEFAULT_PORT, artdata=None, lan: bool = False):
        self.port = port
        self.artdata = artdata
        self.lan = lan                       # True: other computers on the network can open the pages
        self._data = b'{"stops": []}'
        self._named = {"monitor": b'{"rows": []}', "pitlog": b'{"stops": []}', "crew": b'{"enabled": false}'}
        self._lock = threading.Lock()
        self._httpd = None
        self.radio = None                    # the scanner (set by the app), for phones
        self.desk = None                     # crew chief on a phone (beta): set by the app when beta.txt is there

    def publish(self, data: dict):
        body = json.dumps(data, separators=(",", ":")).encode()
        with self._lock:
            self._data = body

    def publish_named(self, name: str, data: dict):
        body = json.dumps(data, separators=(",", ":")).encode()
        with self._lock:
            self._named[name] = body

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _radio(self, path):
                query = urllib.parse.parse_qs(self.path.partition("?")[2])
                radio = server.radio
                if radio is None or self.client_address[0] != "127.0.0.1":
                    self._send(404, b"{}", "application/json")
                elif path == "/radio.json":
                    info = radio.voice.clips_info(query.get("listen") == ["1"])
                    self._send(200, json.dumps(info, separators=(",", ":")).encode(), "application/json")
                elif path == "/radio/phone_only":
                    radio.cfg["phone_only"] = query.get("on") == ["1"]     # the Scanner tab picks it up and saves
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    try:
                        wav = radio.voice.clip_wav(int(path[len("/radio/"):-len(".wav")]))
                    except ValueError:
                        wav = None
                    if wav is None:
                        self._send(404, b"{}", "application/json")
                    else:
                        self._send(200, wav, "audio/wav")

            def do_GET(self):
                path = self.path.split("?", 1)[0].rstrip("/")
                if path == "/radio.json" or path.startswith("/radio/"):
                    self._radio(path)
                elif path.startswith("/crew/"):            # crew chief on a phone (beta): this PC only
                    query = {k: v[0] for k, v in urllib.parse.parse_qs(self.path.partition("?")[2]).items()}
                    desk, what = server.desk, path[len("/crew/"):]
                    if desk is None or self.client_address[0] != "127.0.0.1":
                        self._send(404, b"{}", "application/json")
                        return
                    if what == "claim":
                        out = desk.claim(query.get("car", ""), query.get("name", ""))
                    elif what == "ping":
                        out = desk.ping(query.get("token", ""))
                    elif what == "release":
                        out = desk.release(query.get("token", ""))
                    elif what == "plan":
                        out = desk.plan(query.get("token", ""), query.get("lap", ""), query.get("service", ""),
                                        query.get("fuel", ""), query.get("clear", ""))
                    elif what == "adjust":
                        out = desk.adjust(query.get("token", ""), query.get("wedge", 0), query.get("bar", 0),
                                          query.get("air", 0), query.get("tape", 0), query.get("clear", ""))
                    elif what == "call":
                        out = desk.call(query.get("token", ""), query.get("caution"), query.get("pit"),
                                        query.get("service", ""), query.get("fuel", ""))
                    else:
                        out = {"ok": False, "error": "unknown"}
                    self._send(200, json.dumps(out).encode(), "application/json")
                elif path == "/crew.json":                 # crew chief on a phone (beta): this PC only
                    if self.client_address[0] != "127.0.0.1":
                        self._send(404, b"{}", "application/json")
                    else:
                        with server._lock:
                            body = server._named["crew"]
                        self._send(200, body, "application/json")
                elif path == "/pits":
                    with server._lock:
                        body = server._data
                    self._send(200, body, "application/json")
                elif path in ("/monitor.json", "/pitlog.json"):
                    with server._lock:
                        body = server._named[path[1:-5]]
                    self._send(200, body, "application/json")
                elif path == "/overlay":
                    self._send(200, WIDGET_HTML.encode("utf-8"), "text/html; charset=utf-8")
                elif path in ("", "/live", "/index.html"):
                    self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
                elif path.startswith("/asset/") and server.artdata is not None:
                    rel = urllib.parse.unquote(path[len("/asset/"):])
                    full = server.artdata.abs_path(rel) if rel.lower().endswith((".png", ".jpg", ".svg")) else None
                    if full:
                        with open(full, "rb") as f:
                            body = f.read()
                        self._send(200, body, mimetypes.guess_type(full)[0] or "application/octet-stream")
                    else:
                        self._send(404, b"{}", "application/json")
                else:
                    self._send(404, b"{}", "application/json")

        class Server(ThreadingHTTPServer):
            allow_reuse_address = False          # on Windows reuse lets two AIMODs share the port silently
            daemon_threads = True

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        try:
            self._httpd = Server(("0.0.0.0" if self.lan else "127.0.0.1", self.port), Handler)
        except OSError as e:
            log.warning("Pit board server could not start on port %d (is another AIMOD running?): %s",
                        self.port, e)
            return False
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        log.info("AIMOD Live on http://127.0.0.1:%d/%s", self.port,
                 f" and http://{lan_ip()}:{self.port}/ (network)" if self.lan else "")
        return True

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def restart(self, lan: bool) -> bool:
        self.stop()
        self.lan = lan
        return self.start()


def lan_ip() -> str:
    """This PC's address on the local network (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.168.1.1", 9))
            return sk.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
