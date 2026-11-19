"""A Shelly relay (Gen2 or later) on a second car charger, read over the home network.

Uses the Shelly's own local web API: no cloud, no app, no login (unless a password has been set
on the Shelly, which this doesn't support yet). Read-only for now: it reports whether the relay is
on, and how much power is going through it. Shellys with a power meter (the "PM" models) measure
that; plain ones (e.g. Shelly 1 Gen4) don't, so "on" is taken to mean drawing the configured watts.

    python3 -m homeenergy.shelly 192.168.1.110
"""
import json
import sys
import time
import urllib.request


class ShellySwitch:
    def __init__(self, host, watts=2100, switch_id=0):
        self.host, self.watts, self.id = host, watts, switch_id

    def _rpc(self, method, **params):
        q = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"http://{self.host}/rpc/{method}" + (f"?{q}" if q else "")
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)

    def schedule(self):
        """The Shelly's own daily on/off times for this switch, e.g. [("on", "23:30"), ("off", "05:30")]."""
        out = []
        for job in self._rpc("Schedule.List").get("jobs", []):
            parts = job.get("timespec", "").split()
            if not job.get("enable") or len(parts) != 6 or parts[3:] != ["*", "*", "*"]:
                continue                                  # only simple every-day times
            for call in job.get("calls", []):
                p = call.get("params") or {}
                if call.get("method") == "Switch.Set" and p.get("id", 0) == self.id and "on" in p:
                    out.append(("on" if p["on"] else "off", f"{int(parts[2]):02d}:{int(parts[1]):02d}"))
        return sorted(out, key=lambda x: x[1])

    def status(self):
        s = self._rpc("Switch.GetStatus", id=self.id)
        on = bool(s.get("output"))
        try:
            sched = self.schedule()
        except Exception:
            sched = None
        measured = s.get("apower")                    # only on models with a power meter
        return {
            "time": time.time(),
            "on": on,
            "power_w": round(measured) if measured is not None else (self.watts if on else 0),
            "measured": measured is not None,
            "temp_c": (s.get("temperature") or {}).get("tC"),
            "schedule": sched,
        }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 -m homeenergy.shelly <address>")
    print(ShellySwitch(sys.argv[1]).status())
