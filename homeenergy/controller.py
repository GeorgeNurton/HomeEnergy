"""Battery control around Intelligent Octopus Go's extra (daytime) cheap slots.

The rule:
  - An "extra" car slot (one reaching outside the 23:30-05:30 night window) AND the Ohme says
    the car is actually charging -> the whole house is on the cheap rate, so:
      * HOLD: battery discharge limit (HR 112) 0%, so it doesn't drain into the car, and
      * CHARGE (if grid_charge is on): charge slot 1 (HR 94/95) borrowed from now to the slot's
        end, so the battery fills from the grid at the cheap rate. (This inverter has no usable
        slot 2: it refused writes to HR 31/32, tested 2026-11-12.)
  - Otherwise NORMAL: discharge limit restored, slot 1 back to the night slot (23:30-05:30),
    which does the overnight charge as it always has.
  - Any data missing or stale -> NORMAL.

Safety:
  - Every step re-checks the inverter's real settings and corrects them, so a missed write heals.
  - On startup, a discharge limit stuck at 0% or a borrowed slot 1 is put back to normal.
  - Slot 1 is kept at the configured night slot: changing it in the phone app gets undone.
    Change night_slot in config.toml instead.
  - If the program dies while charging, slot 1 is left on daytime times, so the night charge would
    be missed until it restarts. The phone app puts it right by hand (charge slot 23:30-05:30,
    discharge rate 50%).
"""
import time
from collections import deque
from datetime import datetime, timezone

STALE_S = 120
DISCHARGE_REG = 112
SLOT1_START, SLOT1_END = 94, 95
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
        return "normal", "night slot - the inverter's own night charge covers it", None
    if not ohme or now_s - ohme["time"] > STALE_S:
        return "normal", "extra slot, but no fresh Ohme reading", None
    if not ohme.get("charging"):
        return "normal", "extra slot, car not charging yet", None
    end = _t(slot["end"])
    return "hold", f"extra slot until {end.astimezone():%H:%M} and car charging", end


class Controller:
    def __init__(self, inverter=None, live=False, grid_charge=False, night_slot=(2330, 530),
                 normal_limit=DEFAULT_NORMAL_LIMIT, on_event=None):
        self.inverter = inverter
        self.live = live and inverter is not None   # False = dry run: log only, never write
        self.grid_charge = grid_charge
        self.night_start, self.night_end = night_slot
        self.normal_limit = normal_limit            # learned from the inverter when not holding
        self.on_event = on_event                    # called with each log entry (permanent log)
        self.action = "normal"
        self.reason = "starting"
        self.slot_end = None
        self.since = time.time()
        self.last_error = None
        self.log = deque(maxlen=50)                 # recent changes, newest last

    def _note(self, snap, done, level="info"):
        inv = snap.get("latest") or {}
        e = {"time": time.time(), "level": level, "to": self.action, "reason": self.reason, "done": done,
             "battery_pct": inv.get("battery_pct"), "battery_w": inv.get("battery_w")}
        self.log.append(e)
        if self.on_event:
            self.on_event(e)

    def step(self, snap, now=None):
        now = now or datetime.now(timezone.utc)
        action, reason, end = decide(snap, now)
        if action == "hold" and self.grid_charge:
            action, reason = "charge", reason + " - charging the battery from the grid"
        elif action == "hold":
            reason += " - keeping the battery for later"
        self.reason, self.slot_end = reason, end
        if action != self.action:
            self.action, self.since = action, time.time()
            self._note(snap, f"now {action}" + ("" if self.live else " (dry run - nothing changed)"))
        if self.live:
            self._enforce(snap, now)
        return self.status()

    def _write(self, snap, reg, value, what):
        try:
            got = self.inverter.write(reg, value)
            if got == value:
                self._note(snap, what)
                return True
            self.last_error = f"inverter refused {what}: read back {got}"
        except Exception as e:
            self.last_error = f"{what} failed: {type(e).__name__}: {e}"
        self._note(snap, self.last_error + " - will retry", level="error")
        return False

    def _enforce(self, snap, now):
        """Make the inverter's real settings match the decision. Runs every step, so it self-heals."""
        st = (snap.get("latest") or {}).get("settings") or {}
        limit, s1_start, s1_end = st.get("discharge_limit"), st.get("slot1_start"), st.get("slot1_end")
        if limit is None or s1_start is None:
            return
        ok = True

        # Discharge limit: 0 while holding or charging, otherwise back to what it was.
        if self.action in ("hold", "charge"):
            if limit != 0:
                self.normal_limit = limit
                ok &= self._write(snap, DISCHARGE_REG, 0, "pause battery discharge")
        elif limit == 0:
            ok &= self._write(snap, DISCHARGE_REG, self.normal_limit or DEFAULT_NORMAL_LIMIT,
                              f"restore discharge limit {self.normal_limit}%")
        else:
            self.normal_limit = limit

        # Charge slot 1: borrowed from now to the end of the extra slot, otherwise the night slot.
        if self.action == "charge" and self.slot_end:
            want_end = _hhmm(self.slot_end)
            ours = s1_end == want_end and s1_start != self.night_start
            if not ours:
                start = _hhmm(now)
                ok &= self._write(snap, SLOT1_START, start, f"set charge slot start {start:04d}")
                ok &= self._write(snap, SLOT1_END, want_end, f"set charge slot end {want_end:04d}")
        else:
            if s1_start != self.night_start:
                ok &= self._write(snap, SLOT1_START, self.night_start,
                                  f"restore night charge start {self.night_start:04d}")
            if s1_end != self.night_end:
                ok &= self._write(snap, SLOT1_END, self.night_end,
                                  f"restore night charge end {self.night_end:04d}")

        if ok:
            self.last_error = None

    def status(self):
        return {"mode": "live" if self.live else "dry run", "action": self.action,
                "reason": self.reason, "since": self.since, "error": self.last_error,
                "grid_charge": self.grid_charge, "normal_limit": self.normal_limit,
                "night_slot": f"{self.night_start:04d}-{self.night_end:04d}",
                "log": list(self.log)}
