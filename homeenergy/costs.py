"""Running cost, and what the solar and battery saved, from the inverter's own readings.

Every inverter reading (about every 10 s) is turned into energy and money:
  - grid import, priced at whatever the house is paying at that moment (cheap or full rate)
  - grid export, at the export price in config.toml (0 if you're not paid for export)
  - savings: energy the house used that did NOT come from the grid (solar + battery), valued at
    the price at that moment, minus what it cost to charge the battery from the grid. In other
    words: what the house would have paid with no solar or battery, minus what it actually paid.

Full-price imports are also split by WHY the battery didn't cover them, to judge whether a bigger
battery would pay:
  - empty:  battery at its reserve - more battery would have covered this
  - capped: battery already discharging flat out - a bigger *inverter* would, more battery wouldn't
  - held:   battery deliberately paused (extra car slot) - fine
  - other:  small gaps while the inverter catches up with a change in load
Plus the sunshine that arrived when the battery was already full (some of it heats the water via
the iBoost) - the most a bigger battery could also have caught from the sun.

These are estimates from the inverter's clamp; the Octopus bill is the real figure. Totals per
day are kept in a small JSON file, saved every few minutes.
"""
import json
import threading
import time
from datetime import date, timedelta
from pathlib import Path

MAX_GAP_S = 60          # don't count across gaps longer than this (program stopped, etc.)
SAVE_EVERY_S = 300
KEEP_DAYS = 800

FIELDS = ("import_cheap_kwh", "import_cheap_p", "import_peak_kwh", "import_peak_p",
          "export_kwh", "export_p", "house_kwh", "solar_kwh", "saved_p", "unpriced_kwh",
          "peak_empty_kwh", "peak_capped_kwh", "peak_held_kwh", "peak_other_kwh", "solar_full_kwh",
          "batt_in_kwh", "batt_out_kwh")
EMPTY_MARGIN_PCT = 2      # within this of the reserve counts as empty
CAPPED_FRACTION = 0.85    # discharging at 85%+ of the most ever seen counts as flat out
FULL_PCT = 98
ROUND_TRIP = 0.9          # battery charge/discharge efficiency, roughly


class CostTracker:
    def __init__(self, path, standing_p=0.0, export_p=0.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.standing_p = standing_p
        self.export_p = export_p
        self.lock = threading.Lock()
        self.days = self._load()
        self._last_time = None
        self._last_save = time.time()
        # Most the battery has been seen to discharge / charge at (W), kept with the days
        self.max_out_w = self.days.pop("_max_out_w", 0)
        self.max_in_w = self.days.pop("_max_in_w", 0)

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self):
        with self.lock:
            keep = sorted(self.days)[-KEEP_DAYS:]
            data = {k: self.days[k] for k in keep}
            data["_max_out_w"], data["_max_in_w"] = self.max_out_w, self.max_in_w
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=0), encoding="utf-8")
        tmp.replace(self.path)  # never leaves a half-written file
        self._last_save = time.time()

    def _why_peak(self, reading, held):
        """Why was this full-price import not covered by the battery?"""
        if held:
            return "held"
        pct = reading["battery_pct"]
        reserve = (reading.get("settings") or {}).get("reserve_pct") or 4
        if pct <= reserve + EMPTY_MARGIN_PCT:
            return "empty"
        if self.max_out_w and reading["battery_w"] >= CAPPED_FRACTION * self.max_out_w:
            return "capped"
        return "other"

    def add(self, reading, price_p, cheap, held=False):
        """Count one inverter reading. price_p None = Octopus not known yet (counted, not priced).
        held = the battery control was deliberately pausing the battery."""
        t = reading["time"]
        if self._last_time is None or t <= self._last_time:
            self._last_time = t
            return
        dt = t - self._last_time
        self._last_time = t
        if dt > MAX_GAP_S:
            return
        h = dt / 3600
        grid = reading["grid_w"]                 # + importing, - exporting
        imp = max(grid, 0) * h / 1000
        exp = max(-grid, 0) * h / 1000
        house = max(reading["house_w"], 0) * h / 1000
        solar = max(reading["solar_w"], 0) * h / 1000
        bw = reading.get("battery_w", 0)          # + discharging, - charging
        self.max_out_w = max(self.max_out_w, bw)
        self.max_in_w = max(self.max_in_w, -bw)
        key = time.strftime("%Y-%m-%d", time.localtime(t))
        with self.lock:
            if key not in self.days:
                lt = time.localtime(t)
                self.days[key] = {f: 0.0 for f in FIELDS}
                # started counting after midnight: today's totals are only part of the day
                self.days[key]["late_start"] = lt.tm_hour * 60 + lt.tm_min > 5
                self.days[key]["why_full_day"] = not self.days[key]["late_start"]
            d = self.days[key]
            for f in FIELDS:
                d.setdefault(f, 0.0)
            d["house_kwh"] += house
            d["solar_kwh"] += solar
            d["batt_out_kwh"] += max(bw, 0) * h / 1000
            d["batt_in_kwh"] += max(-bw, 0) * h / 1000
            if reading["battery_pct"] >= FULL_PCT:
                d["solar_full_kwh"] += solar
            d["batt_min_pct"] = min(d.get("batt_min_pct", 100), reading["battery_pct"])
            d["export_kwh"] += exp
            d["export_p"] += exp * self.export_p
            if price_p is None:
                d["unpriced_kwh"] += imp
            else:
                band = "cheap" if cheap else "peak"
                d[f"import_{band}_kwh"] += imp
                d[f"import_{band}_p"] += imp * price_p
                if band == "peak" and imp > 0:
                    d[f"peak_{self._why_peak(reading, held)}_kwh"] += imp
                # What the house would have paid with no solar/battery, minus what it did pay.
                d["saved_p"] += (house - imp) * price_p + exp * self.export_p
        if time.time() - self._last_save > SAVE_EVERY_S:
            self.save()

    def _day(self, key):
        d = self.days.get(key)
        if not d:
            return None
        spent = d["import_cheap_p"] + d["import_peak_p"] + self.standing_p - d["export_p"]
        return {"date": key, "cheap_p": round(d["import_cheap_p"], 1), "peak_p": round(d["import_peak_p"], 1),
                "cheap_kwh": round(d["import_cheap_kwh"], 2), "peak_kwh": round(d["import_peak_kwh"], 2),
                "export_kwh": round(d["export_kwh"], 2), "house_kwh": round(d["house_kwh"], 2),
                "solar_kwh": round(d["solar_kwh"], 2), "saved_p": round(d["saved_p"], 1),
                "standing_p": self.standing_p, "spent_p": round(spent, 1),
                "peak_why": {k: round(d.get(f"peak_{k}_kwh", 0), 2) for k in ("empty", "capped", "held", "other")},
                "solar_full_kwh": round(d.get("solar_full_kwh", 0), 2),
                "batt_out_kwh": round(d.get("batt_out_kwh", 0), 2),
                "batt_min_pct": d.get("batt_min_pct"),
                "partial": d.get("unpriced_kwh", 0) > 0.05 or bool(d.get("late_start"))}

    def summary(self):
        today = date.today()
        with self.lock:
            days = [self._day((today - timedelta(days=i)).isoformat()) for i in range(14)]

            def total(start):
                ks = [k for k in self.days if k >= start.isoformat() and not k.startswith("_")]
                rows = [self._day(k) for k in ks]
                return {"spent_p": round(sum(r["spent_p"] for r in rows), 1),
                        "cheap_p": round(sum(r["cheap_p"] for r in rows), 1),
                        "peak_p": round(sum(r["peak_p"] for r in rows), 1),
                        "saved_p": round(sum(r["saved_p"] for r in rows), 1),
                        "days": len(rows)}

            week = total(today - timedelta(days=today.weekday()))
            month = total(today.replace(day=1))
            bigger = self._bigger_battery(today)
        return {"today": days[0], "days": [d for d in days if d], "week": week, "month": month,
                "bigger_battery": bigger}

    def _bigger_battery(self, today):
        """What extra battery would have saved, from complete days since the 'why' tracking began.
        Each kWh of 'empty' full-price import could instead have come from a battery charged at
        night (losing ~10% on the way). Ignores the extra sun it could also catch (shown separately)."""
        rows = []
        for k in sorted(self.days):
            if k.startswith("_") or k >= today.isoformat():
                continue                                   # today isn't finished
            d = self.days[k]
            if not d.get("why_full_day"):
                continue
            rows.append(d)
        rows = rows[-60:]                                   # the last two months
        out = {"days": len(rows), "max_out_w": self.max_out_w, "max_in_w": self.max_in_w}
        if not rows:
            return out
        n = len(rows)

        def avg(f):
            return sum(r.get(f, 0) for r in rows) / n

        peak_kwh = sum(r["import_peak_kwh"] for r in rows)
        cheap_kwh = sum(r["import_cheap_kwh"] for r in rows)
        peak_p = sum(r["import_peak_p"] for r in rows) / peak_kwh if peak_kwh > 0.5 else 37.7
        cheap_p = sum(r["import_cheap_p"] for r in rows) / cheap_kwh if cheap_kwh > 0.5 else 6.5
        gain_p = peak_p - cheap_p / ROUND_TRIP               # saved per kWh moved to the night
        options = []
        for extra in (5, 10):
            per_day = sum(min(extra * ROUND_TRIP, r.get("peak_empty_kwh", 0)) for r in rows) / n
            options.append({"extra_kwh": extra, "kwh_per_day": round(per_day, 2),
                            "p_per_day": round(per_day * gain_p, 1),
                            "pounds_per_year": round(per_day * gain_p * 365 / 100)})
        out.update({"empty_kwh_per_day": round(avg("peak_empty_kwh"), 2),
                    "capped_kwh_per_day": round(avg("peak_capped_kwh"), 2),
                    "held_kwh_per_day": round(avg("peak_held_kwh"), 2),
                    "other_kwh_per_day": round(avg("peak_other_kwh"), 2),
                    "solar_full_kwh_per_day": round(avg("solar_full_kwh"), 2),
                    "gain_p_per_kwh": round(gain_p, 1), "options": options})
        return out
