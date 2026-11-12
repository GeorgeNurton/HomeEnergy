"""Battery control: stop the battery feeding the car during extra (daytime) cheap slots.

The rule:
  - Night 23:30-05:30: leave the inverter alone. Its own 00:30-05:30 charge handles it.
  - An "extra" car slot (one reaching outside the night window) AND the Ohme says the car
    is actually charging -> HOLD the battery (no discharging). Grid power is cheap then,
    so stored energy is better saved for the evening.
  - Anything else, or any data missing/stale -> NORMAL (eco mode as the inverter always runs).

Failing safe always means NORMAL: if the Pi dies or loses a source, the house behaves
exactly as it did before the Pi existed.

DRY RUN only for now: it decides and logs what it would do; it never writes to the inverter.
"""
import time
from collections import deque
from datetime import datetime, timezone

STALE_S = 120


def _t(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def decide(snap, now=None):
    """Pure function: given a state snapshot, return (action, reason)."""
    now = now or datetime.now(timezone.utc)
    now_s = now.timestamp()
    inv = snap.get("latest")
    ohme = snap.get("ohme")
    planned = (snap.get("dispatches") or {}).get("planned") or []

    if not inv or now_s - inv["time"] > STALE_S:
        return "normal", "no fresh inverter reading"
    slot = next((p for p in planned if _t(p["start"]) <= now < _t(p["end"])), None)
    if not slot:
        return "normal", "no car slot now"
    if not slot.get("extra"):
        return "normal", "night slot - inverter's own schedule handles it"
    if not ohme or now_s - ohme["time"] > STALE_S:
        return "normal", "extra slot, but no fresh Ohme reading"
    if not ohme.get("charging"):
        return "normal", "extra slot, car not charging yet"
    end = _t(slot["end"]).astimezone().strftime("%H:%M")
    return "hold", f"extra slot until {end} and car charging - keep the battery for later"


class Controller:
    def __init__(self, live=False):
        self.live = live              # False = dry run: log only, never write
        self.action = "normal"
        self.reason = "starting"
        self.since = time.time()
        self.log = deque(maxlen=50)   # recent changes, newest last

    def step(self, snap, now=None):
        action, reason = decide(snap, now)
        self.reason = reason
        if action != self.action:
            inv = snap.get("latest") or {}
            self.log.append({
                "time": time.time(), "from": self.action, "to": action, "reason": reason,
                "battery_pct": inv.get("battery_pct"), "battery_w": inv.get("battery_w"),
                "done": "would " + ("pause battery discharge" if action == "hold" else "restore normal eco")
                        + (" (dry run)" if not self.live else ""),
            })
            self.action, self.since = action, time.time()
            # Live writes to the inverter go here once dry run has been checked.
        return self.status()

    def status(self):
        return {"mode": "live" if self.live else "dry run", "action": self.action,
                "reason": self.reason, "since": self.since, "log": list(self.log)}
