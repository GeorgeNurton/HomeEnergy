"""Battery control: stop the battery feeding the car during extra (daytime) cheap slots.

The rule:
  - An "extra" car slot (one reaching outside the 23:30-05:30 night window) AND the Ohme says
    the car is actually charging -> HOLD the battery (discharge limit 0%). Grid power is cheap
    then, so stored energy is better saved for the evening.
  - Night 23:30-05:30 is left to the inverter: its own grid charge runs 23:30-05:30 (set
    2026-11-12) and a battery doesn't discharge during its charge slot.
  - Anything else, or any data missing/stale -> NORMAL.

How HOLD is done: battery discharge limit (holding register 112) set to 0%, then put back to
what it was (normally 50%). Safety:
  - Every step re-checks the inverter's real setting and corrects it, so a missed write heals.
  - On startup, a limit found stuck at 0% is restored (e.g. after a crash mid-hold).
  - If the program dies mid-hold, the battery stays paused until restarted. The phone app's
    battery discharge setting puts it right by hand.
"""
import time
from collections import deque
from datetime import datetime, timezone

STALE_S = 120
DISCHARGE_REG = 112
DEFAULT_NORMAL_LIMIT = 50


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
        return "normal", "night slot - the inverter's own 23:30 charge covers it"
    if not ohme or now_s - ohme["time"] > STALE_S:
        return "normal", "extra slot, but no fresh Ohme reading"
    if not ohme.get("charging"):
        return "normal", "extra slot, car not charging yet"
    end = _t(slot["end"]).astimezone().strftime("%H:%M")
    return "hold", f"extra slot until {end} and car charging - keep the battery for later"


class Controller:
    def __init__(self, inverter=None, live=False, normal_limit=DEFAULT_NORMAL_LIMIT):
        self.inverter = inverter
        self.live = live and inverter is not None   # False = dry run: log only, never write
        self.normal_limit = normal_limit            # learned from the inverter when not holding
        self.action = "normal"
        self.reason = "starting"
        self.since = time.time()
        self.last_error = None
        self.log = deque(maxlen=50)                 # recent changes, newest last

    def _note(self, snap, frm, to, reason, done):
        inv = snap.get("latest") or {}
        self.log.append({"time": time.time(), "from": frm, "to": to, "reason": reason, "done": done,
                         "battery_pct": inv.get("battery_pct"), "battery_w": inv.get("battery_w")})

    def step(self, snap, now=None):
        action, reason = decide(snap, now)
        self.reason = reason
        if action != self.action:
            if not self.live:
                self._note(snap, self.action, action, reason,
                           "would " + ("pause battery discharge" if action == "hold" else "restore normal")
                           + " (dry run)")
            self.action, self.since = action, time.time()
        if self.live:
            self._enforce(snap)
        return self.status()

    def _enforce(self, snap):
        """Make the inverter's real setting match the decision. Runs every step, so it self-heals."""
        current = ((snap.get("latest") or {}).get("settings") or {}).get("discharge_limit")
        if current is None:
            return
        if self.action == "hold":
            want = 0
            if current != 0:
                self.normal_limit = current          # remember what to put back
        else:
            if current != 0:
                self.normal_limit = current          # not ours to change; just remember it
                return
            want = self.normal_limit or DEFAULT_NORMAL_LIMIT
        if current == want:
            return
        try:
            got = self.inverter.write(DISCHARGE_REG, want)
            ok = got == want
            self.last_error = None if ok else f"wrote {want}, read back {got}"
            self._note(snap, "", self.action, self.reason,
                       ("paused battery discharge" if want == 0 else f"restored discharge limit {want}%")
                       + ("" if ok else f" - CHECK: read back {got}"))
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._note(snap, "", self.action, self.reason, f"write failed, will retry: {self.last_error}")

    def status(self):
        return {"mode": "live" if self.live else "dry run", "action": self.action,
                "reason": self.reason, "since": self.since, "error": self.last_error,
                "normal_limit": self.normal_limit, "log": list(self.log)}
