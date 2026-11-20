"""Run the granny charger in extra cheap daytime slots.

The night (23:30-05:30) is handled by the Shelly's own timer, so it works even if the Pi is off.
This adds the extra car slots Octopus sometimes gives outside the night: when the house is on the
cheap rate (a slot is running AND the Ohme car is actually charging), switch the Shelly on with
its own built-in "flip back off after N seconds" timer set to the end of the slot. So if the Pi or
the WiFi dies mid-slot, the Shelly still switches itself off on time.

The Pi only ever switches off what it switched on (it remembers when it started the Shelly's
timer), so turning the charger on yourself from the Shelly app is left alone.
"""
import time
from datetime import datetime, timezone

END_EARLY_S = 60        # stop a minute before the slot ends (and before the night timer's 23:30 on)
MIN_RUN_S = 120         # not worth starting for less than this
RESYNC_S = 90           # re-send if the slot's end moved by more than this


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class GrannyControl:
    def __init__(self, shelly, live=True, on_event=None):
        self.shelly, self.live = shelly, live
        self.on_event = on_event or (lambda text, level="info": None)
        self.mine = None          # (timer started at, ends at) of the timer this program set
        self.said = None          # last dry-run message, so it isn't repeated every 30 s

    def _slot_end(self, snap, now):
        """End of the extra slot running now, if the house is on the cheap rate in it."""
        cheap = snap.get("cheap") or {}
        if not cheap.get("now"):
            return None
        for e in snap.get("extras") or []:
            if _ts(e["start"]) <= now < _ts(e["end"]):
                return _ts(e["end"])
        return None

    def step(self, snap, now=None):
        now = now or time.time()
        g = snap.get("granny")
        if not g or now - g.get("time", 0) > 120:
            return {"action": "none", "reason": "no fresh Shelly reading"}
        end = self._slot_end(snap, now)
        timer_at, timer_len = g.get("timer_started_at"), g.get("timer_duration")
        is_mine = bool(self.mine and timer_at and abs(timer_at - self.mine[0]) < 5)

        if end is not None:
            run = end - END_EARLY_S - now
            if run < MIN_RUN_S:
                return {"action": "none", "reason": "slot nearly over"}
            if g["on"] and not is_mine and not timer_at:
                return {"action": "none", "reason": "already on (night timer or switched on by hand)"}
            if is_mine and abs((timer_at + timer_len) - (end - END_EARLY_S)) < RESYNC_S:
                return {"action": "on", "reason": "extra slot, already running"}
            until = time.strftime("%H:%M", time.localtime(end - END_EARLY_S))
            if not self.live:
                msg = f"granny charger would switch on until {until} (extra slot, car charging) - dry run"
                if msg != self.said:
                    self.said = msg
                    self.on_event(msg)
                return {"action": "on (dry run)", "reason": msg}
            started = self.shelly.set(True, toggle_after=round(run))
            self.mine = (started, end - END_EARLY_S)
            self.on_event(f"granny charger on until {until} (extra cheap slot, car charging)")
            return {"action": "on", "reason": f"extra slot until {until}"}

        # Not a cheap extra slot: undo only our own run (e.g. the car stopped charging early)
        if g["on"] and is_mine:
            if self.live:
                self.shelly.set(False)
                self.on_event("granny charger off (cheap slot over or car stopped charging)")
            self.mine = None
            return {"action": "off", "reason": "cheap slot over"}
        self.said = None
        return {"action": "none", "reason": ""}
