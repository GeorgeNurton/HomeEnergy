"""Polls the inverter in the background and serves the phone dashboard.

Standard library only, so the Pi needs nothing installed beyond Python.
Run:  python -m homeenergy  (settings in config.toml)
"""
import json
import logging
import threading
import time
import tomllib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .givenergy import Inverter

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("homeenergy")


class State:
    """Latest reading plus a day of one-minute history, held in memory only
    (keeps the SD card from being written to all day)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latest = None
        self.error = None
        self.history = deque(maxlen=24 * 60)
        self._last_minute = None

    def update(self, reading):
        with self.lock:
            self.latest, self.error = reading, None
            minute = int(reading["time"] // 60)
            if minute != self._last_minute:
                self._last_minute = minute
                self.history.append({k: reading[k] for k in
                                     ("time", "solar_w", "house_w", "grid_w", "battery_w", "battery_pct")})

    def fail(self, msg):
        with self.lock:
            self.error = msg

    def snapshot(self):
        with self.lock:
            return {"latest": self.latest, "error": self.error, "history": list(self.history)}


def poller(inverter, state, interval):
    while True:
        try:
            state.update(inverter.status())
        except Exception as e:  # keep going whatever happens; the dongle can be flaky
            log.warning("inverter read failed: %s", e)
            state.fail(f"{type(e).__name__}: {e}")
        time.sleep(interval)


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                self._send(200, json.dumps(state.snapshot()).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args):
            pass

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    state = State()
    inverter = Inverter(cfg["givenergy"]["host"])
    threading.Thread(target=poller, args=(inverter, state, cfg["givenergy"].get("poll_seconds", 10)),
                     daemon=True).start()
    port = cfg["web"].get("port", 8080)
    log.info("dashboard on http://0.0.0.0:%d", port)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(state)).serve_forever()
