"""Battery control around Intelligent Octopus Go's extra (daytime) cheap slots.

The rule:
  - An "extra" car slot (one reaching outside the 23:30-05:30 night window) AND the Ohme says
    the car is actually charging -> the whole house is on the cheap rate, so:
      * HOLD: battery discharge limit (HR 112) 0%, so it doesn't drain into the car, and
      * CHARGE (if grid_charge is on): charge slot 2 (HR 31/32) set from now to the slot's end,
        so the battery fills from the grid at the cheap rate.
  - Night 23:30-05:30 is left to the inverter: its own slot 1 grid charge runs 23:30-05:30.
  - Anything else, or any data missing/stale -> NORMAL: discharge limit restored, slot 2 cleared.

Safety:
  - Every step re-checks the inverter's real settings and corrects them, so a missed write heals.
  - On startup, a discharge limit stuck at 0% or a leftover slot 2 is put back to normal.
  - Slot 2 belongs to this program: anything set there by hand gets cleared.
  - If the program dies mid-slot, the battery stays paused and slot 2 repeats daily until it
    restarts. The phone app puts both right by hand (discharge rate 50%, charge slot 2 00:00-00:00).
"""
import time
from collections import deque
from datetime import datetime, timezone

STALE_S = 120
DISCHARGE_REG = 112
SLOT2_START, SLOT2_END = 31, 32
SLOT2_OFF = 0  # 00:00-00:00 = slot not used
DEFAULT_NORMAL_LIMIT = 50


def _t(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _hhmm(dt):
    local = dt.astimezone()  # the inverter's clock runs on UK local time
    return local.hour * 100 + local.minute


def decide(snap, now=None):
    """Pure function: given a state snapshot, return (action, reason, slot_end or None)."""
    now = now or datetime.now(timezone.utc)
    now_s = now.timestamp()
    inv = snap.get("latest")
    ohme = snap.get("ohme")
    planned = (snap.get("dispatches") or {}).get("planned") or []

    if not inv or now_s - inv["time"] > STALE_S:
        return "normal", "no fresh inverter reading", None
    slot = next((p for p in planned if _t(p["start"]) <= now < _t(p["end"])), None)
    if not slot:
        return "normal", "no car slot now", None
    if not slot.get("extra"):
        return "normal", "night slot - the inverter's own 23:30 charge covers it", None
    if not ohme or now_s - ohme["time"] > STALE_S:
        return "normal", "extra slot, but no fresh Ohme reading", None
    if not ohme.get("charging"):
        return "normal", "extra slot, car not charging yet", None
    end = _t(slot["end"])
    return "hold", f"extra slot until {end.astimezone():%H:%M} and car charging", end


class Controller:
    def __init__(self, inverter=None, live=False, grid_charge=False, normal_limit=DEFAULT_NORMAL_LIMIT):
        self.inverter = inverter
        self.live = live and inverter is not None   # False = dry run: log only, never write
        self.grid_charge = grid_charge
        self.normal_limit = normal_limit            # learned from the inverter when not holding
        self.action = "normal"
        self.reason = "starting"
        self.slot_end = None
        self.since = time.time()
        self.last_error = None
        self.log = deque(maxlen=50)                 # recent changes, newest last

    def _note(self, snap, to, done):
        inv = snap.get("latest") or {}
        self.log.append({"time": time.time(), "to": to, "reason": self.reason, "done": done,
                         "battery_pct": inv.get("battery_pct"), "battery_w": inv.get("battery_w")})

    def step(self, snap, now=None):
        now = now or datetime.now(timezone.utc)
        action, reason, end = decide(snap, now)
        if action == "hold" and self.grid_charge:
            action, reason = "charge", reason + " - charging the battery from the grid"
        elif action == "hold":
            reason += " - keeping the battery for later"
        self.reason, self.slot_end = reason, end
        if action != self.action:
            if not self.live:
                self._note(snap, action, f"would switch to {action} (dry run)")
            self.action, self.since = action, time.time()
        if self.live:
            self._enforce(snap, now)
        return self.status()

    def _write(self, snap, reg, value, what):
        try:
            got = self.inverter.write(reg, value)
            ok = got == value
            if not ok:
                self.last_error = f"register {reg}: wrote {value}, read back {got}"
            self._note(snap, self.action, what + ("" if ok else f" - CHECK: read back {got}"))
            return ok
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._note(snap, self.action, f"{what} FAILED, will retry: {self.last_error}")
            return False

    def _enforce(self, snap, now):
        """Make the inverter's real settings match the decision. Runs every step, so it self-heals."""
        st = (snap.get("latest") or {}).get("settings") or {}
        limit, s2_start, s2_end = st.get("discharge_limit"), st.get("slot2_start"), st.get("slot2_end")
        if limit is None or s2_start is None:
            return
        ok = True

        # Discharge limit: 0 while holding or charging, otherwise back to what it was.
        if self.action in ("hold", "charge"):
            if limit != 0:
                self.normal_limit = limit
                ok &= self._write(snap, DISCHARGE_REG, 0, "paused battery discharge")
        elif limit == 0:
            ok &= self._write(snap, DISCHARGE_REG, self.normal_limit or DEFAULT_NORMAL_LIMIT,
                              f"restored discharge limit {self.normal_limit}%")
        else:
            self.normal_limit = limit

        # Charge slot 2: from now to the end of the extra slot while charging, otherwise cleared.
        if self.action == "charge" and self.slot_end:
            want_end = _hhmm(self.slot_end)
            if s2_end != want_end or s2_start == SLOT2_OFF:
                start = _hhmm(now)
                ok &= self._write(snap, SLOT2_START, start, f"charge slot 2 start {start:04d}")
                ok &= self._write(snap, SLOT2_END, want_end,
                                  f"charge slot 2 end {want_end:04d} - battery charging from the grid")
        elif s2_start != SLOT2_OFF or s2_end != SLOT2_OFF:
            ok &= self._write(snap, SLOT2_START, SLOT2_OFF, "cleared charge slot 2 start")
            ok &= self._write(snap, SLOT2_END, SLOT2_OFF, "cleared charge slot 2 end")

        if ok:
            self.last_error = None

    def status(self):
        return {"mode": "live" if self.live else "dry run", "action": self.action,
                "reason": self.reason, "since": self.since, "error": self.last_error,
                "grid_charge": self.grid_charge, "normal_limit": self.normal_limit,
                "log": list(self.log)}
