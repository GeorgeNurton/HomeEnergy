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

from .advice import advise
from .controller import Controller
from .costs import CostTracker
from .givenergy import Inverter
from .octopus import Octopus, cheap_now
from .ohme import Ohme
from .solar import SolarForecast

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("homeenergy")
ALERT_AFTER_S = 5 * 60   # a source failing for this long raises an alert
SOURCE_NAMES = {"inverter": "Inverter", "rates": "Octopus prices", "dispatches": "Octopus slots",
                "ohme": "Ohme charger", "control": "Battery control"}


class EventLog:
    """Permanent log of what the system did and what went wrong: one JSON line per event.
    Only events are written (a handful a day), so it's easy on the SD card."""

    MAX_BYTES = 2_000_000

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def add(self, kind, text, level="info", **extra):
        e = {"time": time.time(), "kind": kind, "level": level, "text": text, **extra}
        with self.lock:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(e) + "\n")
                if self.path.stat().st_size > self.MAX_BYTES:   # keep the newest half
                    lines = self.path.read_text(encoding="utf-8").splitlines(True)
                    self.path.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
            except OSError as err:
                log.warning("could not write event log: %s", err)
        (log.error if level == "error" else log.info)("%s: %s", kind.upper(), text)

    def recent(self, n=300):
        with self.lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out[::-1]


class State:
    """Latest reading from each source plus a day of one-minute inverter history,
    held in memory only (keeps the SD card from being written to all day)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}      # source -> latest result
        self.errors = {}    # source -> last error text (cleared on success)
        self.error_since = {}  # source -> when it started failing
        self.history = deque(maxlen=24 * 60)
        self.alerts_cache = []
        self._last_minute = None

    def set(self, source, value):
        with self.lock:
            self.data[source] = value
            self.errors.pop(source, None)
            self.error_since.pop(source, None)
            if source == "inverter":
                minute = int(value["time"] // 60)
                if minute != self._last_minute:
                    self._last_minute = minute
                    self.history.append({k: value[k] for k in
                                         ("time", "solar_w", "house_w", "grid_w", "battery_w", "battery_pct")})

    def fail(self, source, msg):
        with self.lock:
            self.errors[source] = msg
            self.error_since.setdefault(source, time.time())

    def alerts(self):
        """Problems worth a red banner: anything failing for a while, or a control error."""
        out = []
        with self.lock:
            now = time.time()
            for src, since in self.error_since.items():
                if now - since >= ALERT_AFTER_S:
                    out.append(f"{SOURCE_NAMES.get(src, src)} not answering since "
                               f"{time.strftime('%H:%M', time.localtime(since))}: {self.errors.get(src)}")
            ctl = self.data.get("control") or {}
            if ctl.get("error"):
                out.append(f"Battery control: {ctl['error']}")
            inv = self.data.get("inverter")
            if inv and now - inv["time"] > ALERT_AFTER_S and "inverter" not in self.error_since:
                out.append("Inverter readings have stopped")
        return out

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
                "extras": extra_times(planned, octo_rates),
                "ohme": d.get("ohme"),
                "control": d.get("control"),
                "cheap": {"now": cheap, "price_p": price, "why": why} if octo_rates is not None else None,
                "errors": dict(self.errors),
                "error": self.errors.get("inverter"),
                "history": list(self.history),
                "alerts": list(self.alerts_cache),
                "costs": d.get("costs"),
                "solar_forecast": d.get("solar_forecast"),
            }


def extra_times(planned, rates):
    """The parts of the car's slots that fall OUTSIDE the night cheap window, merged and sorted.
    Those are the only times worth telling people about: the night is cheap anyway."""
    if not rates:
        return []
    low = min(r["p"] for r in rates)
    nights = [(_dt(r["from"]), _dt(r["to"])) for r in rates if r["p"] <= low + 0.01 and r["to"]]
    pieces = []
    for p in planned or []:
        parts = [(_dt(p["start"]), _dt(p["end"]))]
        for nf, nt in nights:
            nxt = []
            for a, b in parts:
                if b <= nf or a >= nt:
                    nxt.append((a, b))
                    continue
                if a < nf:
                    nxt.append((a, nf))
                if b > nt:
                    nxt.append((nt, b))
            parts = nxt
        pieces += [x for x in parts if x[1] > x[0]]
    pieces.sort()
    merged = []
    for a, b in pieces:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return [{"start": a.isoformat(), "end": b.isoformat()} for a, b in merged]


def _dt(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _z(ts):
    """Any ISO time -> 'YYYY-MM-DDTHH:MM:SSZ' in UTC, so times compare as plain text."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poller(name, fn, state, interval, delay=0):
    """Call fn every `interval` seconds forever; one source failing never stops the others."""
    time.sleep(delay)
    while True:
        try:
            state.set(name, fn())
        except Exception as e:
            log.warning("%s failed: %s", name, e)
            state.fail(name, f"{type(e).__name__}: {e}")
        time.sleep(interval)


def _log_page(events):
    rows = []
    for e in events:
        t = time.strftime("%a %d %b %H:%M:%S", time.localtime(e["time"]))
        cls = ' class="err"' if e.get("level") == "error" else ""
        extra = f' <span class="m">battery {e["battery_pct"]}%</span>' if e.get("battery_pct") is not None else ""
        text = str(e.get("text", "")).replace("&", "&amp;").replace("<", "&lt;")
        rows.append(f"<tr{cls}><td>{t}</td><td>{e.get('kind', '')}</td><td>{text}{extra}</td></tr>")
    body = "".join(rows) or "<tr><td>Nothing logged yet</td></tr>"
    return LOG_PAGE.replace("{rows}", body)


LOG_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Home Energy log</title>
<style>
:root { --bg:#f4f5f7; --text:#18202a; --muted:#6b7684; --line:#e3e6ea; --err:#d9480f; }
@media (prefers-color-scheme: dark) { :root { --bg:#0f1720; --text:#eef2f6; --muted:#93a0ae; --line:#263241; --err:#ff8a4c; } }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.4 system-ui,sans-serif; }
main { max-width:760px; margin:0 auto; padding:16px; }
a { color:inherit; } table { width:100%; border-collapse:collapse; }
td { padding:6px 4px; border-bottom:1px solid var(--line); vertical-align:top; }
td:first-child { white-space:nowrap; color:var(--muted); } td:nth-child(2) { color:var(--muted); }
tr.err td { color:var(--err); } .m { color:var(--muted); }
</style></head><body><main><h1>Event log</h1><p><a href="/">&larr; dashboard</a> &middot; newest first</p>
<table>{rows}</table></main></body></html>"""


def make_handler(state, events):
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
            elif self.path == "/log":
                self._send(200, _log_page(events.recent()).encode(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                snap = state.snapshot()
                try:
                    snap["advice"] = advise(snap)
                except Exception as e:  # never let the advice break the dashboard
                    snap["advice"] = {"colour": "grey", "title": "Advice unavailable", "detail": str(e), "next": ""}
                self._send(200, json.dumps(snap).encode(), "application/json")
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
    events = EventLog(ROOT / "logs" / "events.jsonl")
    events.add("start", "HomeEnergy started")

    def start(name, fn, interval, delay=0):
        threading.Thread(target=poller, args=(name, fn, state, interval, delay), daemon=True).start()

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

    sol = cfg.get("solar")
    if sol and sol.get("kwp"):
        fc = SolarForecast(sol["lat"], sol["lon"], sol["tilt"], sol["azimuth"], sol["kwp"],
                           shade_before=sol.get("shade_before", 8), shade_after=sol.get("shade_after", 17))
        start("solar_forecast", fc.fetch, 60 * 60)   # free service: 12 lookups an hour, use 1
    SOURCE_NAMES["solar_forecast"] = "Solar forecast"

    ctl_cfg = cfg.get("control", {})
    night_cfg = str(ctl_cfg.get("night_slot", "2330-0530"))
    night = night_cfg.split("-")

    def on_event(e):
        events.add("control", f'{e["done"]} ({e["reason"]})', level=e["level"],
                   battery_pct=e["battery_pct"], battery_w=e["battery_w"])

    ctrl = Controller(inverter, live=ctl_cfg.get("live", False), grid_charge=ctl_cfg.get("grid_charge", False),
                      night_slot=(int(night[0]), int(night[1])), on_event=on_event)
    events.add("start", "battery control %s, grid charging in extra slots %s, night slot %s" % (
        "LIVE" if ctrl.live else "dry run", "on" if ctrl.grid_charge else "off", night_cfg))

    def control_step():
        result = ctrl.step(state.snapshot())
        state.set("control", result)
        # Raise and clear alerts, logging each change once.
        now_alerts = state.alerts()
        before = set(state.alerts_cache)
        for a in now_alerts:
            if a not in before:
                events.add("alert", a, level="error")
        for a in before - set(now_alerts):
            events.add("alert", "cleared: " + a)
        state.alerts_cache = now_alerts
        return result

    start("control", control_step, 30, delay=20)  # let the first readings arrive

    cost_cfg = cfg.get("costs", {})
    tracker = CostTracker(ROOT / "data" / "daily.json", standing_p=cost_cfg.get("standing_charge_p", 0.0),
                          export_p=cost_cfg.get("export_p", 0.0))
    last_counted = [None]

    def cost_step():
        snap = state.snapshot()
        inv, cheap = snap.get("latest"), snap.get("cheap")
        if inv and inv["time"] != last_counted[0]:
            last_counted[0] = inv["time"]
            tracker.add(inv, (cheap or {}).get("price_p"), (cheap or {}).get("now", False))
        return tracker.summary()

    start("costs", cost_step, 5, delay=15)

    import atexit
    import signal
    import sys
    atexit.register(tracker.save)
    # A normal stop (systemd, Ctrl+C) saves the day's totals on the way out.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    port = cfg["web"].get("port", 8080)
    log.info("dashboard on http://0.0.0.0:%d", port)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(state, events)).serve_forever()
