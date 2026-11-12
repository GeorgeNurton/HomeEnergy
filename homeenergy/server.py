"""Polls the inverter, Octopus and Ohme in the background and serves the phone dashboard.

Standard library only, so the Pi needs nothing installed beyond Python.
Run:  python -m homeenergy  (settings in config.toml, keys in secrets.toml)
"""
import json
import logging
import threading
import time
import tomllib
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .givenergy import Inverter
from .octopus import Octopus, cheap_now
from .ohme import Ohme

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("homeenergy")


class State:
    """Latest reading from each source plus a day of one-minute inverter history,
    held in memory only (keeps the SD card from being written to all day)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}      # source -> latest result
        self.errors = {}    # source -> last error text (cleared on success)
        self.history = deque(maxlen=24 * 60)
        self._last_minute = None

    def set(self, source, value):
        with self.lock:
            self.data[source] = value
            self.errors.pop(source, None)
            if source == "inverter":
                minute = int(value["time"] // 60)
                if minute != self._last_minute:
                    self._last_minute = minute
                    self.history.append({k: value[k] for k in
                                         ("time", "solar_w", "house_w", "grid_w", "battery_w", "battery_pct")})

    def fail(self, source, msg):
        with self.lock:
            self.errors[source] = msg

    def snapshot(self):
        with self.lock:
            d = dict(self.data)
            octo_rates = (d.get("rates") or {}).get("rates")
            planned = (d.get("dispatches") or {}).get("planned")
            cheap, price, why = cheap_now(octo_rates, planned)
            dispatches = d.get("dispatches")
            if dispatches and octo_rates:
                # Mark slots that reach outside the night window: those are the *extra* cheap
                # house time. Slots inside the night window only steer the car.
                low = min(r["p"] for r in octo_rates)
                nights = [(r["from"], r["to"]) for r in octo_rates if r["p"] <= low + 0.01]
                dispatches = dict(dispatches, planned=[
                    dict(p, extra=not any(f <= _z(p["start"]) and _z(p["end"]) <= (t or "9999")
                                          for f, t in nights))
                    for p in planned or []])
            if why == "smart charge slot":
                # A daytime slot only gives the house the cheap rate once the car is actually
                # charging, so check the Ohme too (and don't trust a stale Ohme reading).
                ohme = d.get("ohme") or {}
                fresh = time.time() - ohme.get("time", 0) < 120
                if fresh and ohme.get("charging"):
                    why = "smart slot, car charging"
                else:
                    cheap, why = False, "smart slot, car not charging yet"
                    rates = (d.get("rates") or {}).get("rates") or []
                    price = max((r["p"] for r in rates), default=price)
            return {
                "latest": d.get("inverter"),
                "rates": d.get("rates"),
                "dispatches": dispatches,
                "ohme": d.get("ohme"),
                "cheap": {"now": cheap, "price_p": price, "why": why} if octo_rates is not None else None,
                "errors": dict(self.errors),
                "error": self.errors.get("inverter"),
                "history": list(self.history),
            }


def _z(ts):
    """Any ISO time -> 'YYYY-MM-DDTHH:MM:SSZ' in UTC, so times compare as plain text."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poller(name, fn, state, interval):
    """Call fn every `interval` seconds forever; one source failing never stops the others."""
    while True:
        try:
            state.set(name, fn())
        except Exception as e:
            log.warning("%s failed: %s", name, e)
            state.fail(name, f"{type(e).__name__}: {e}")
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


def _filled(section, *keys):
    return section and all(str(section.get(k, "")).strip() for k in keys)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    secrets_file = ROOT / "secrets.toml"
    secrets = tomllib.loads(secrets_file.read_text()) if secrets_file.exists() else {}
    state = State()

    def start(name, fn, interval):
        threading.Thread(target=poller, args=(name, fn, state, interval), daemon=True).start()

    inverter = Inverter(cfg["givenergy"]["host"])
    start("inverter", inverter.status, cfg["givenergy"].get("poll_seconds", 10))

    octo = secrets.get("octopus")
    if _filled(octo, "api_key", "account"):
        o = Octopus(octo["api_key"], octo["account"])
        start("rates", o.rates, 30 * 60)
        start("dispatches", o.dispatches, 2 * 60)
    else:
        log.info("Octopus not configured (secrets.toml) - skipping")

    oh = secrets.get("ohme")
    if _filled(oh, "email", "password"):
        start("ohme", Ohme(oh["email"], oh["password"]).status, 30)
    else:
        log.info("Ohme not configured (secrets.toml) - skipping")

    port = cfg["web"].get("port", 8080)
    log.info("dashboard on http://0.0.0.0:%d", port)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(state)).serve_forever()
