"""Running cost, and what the solar and battery saved, from the inverter's own readings.

Every inverter reading (about every 10 s) is turned into energy and money:
  - grid import, priced at whatever the house is paying at that moment (cheap or full rate)
  - grid export, at the export price in config.toml (0 if you're not paid for export)
  - savings: energy the house used that did NOT come from the grid (solar + battery), valued at
    the price at that moment, minus what it cost to charge the battery from the grid. In other
    words: what the house would have paid with no solar or battery, minus what it actually paid.

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
          "export_kwh", "export_p", "house_kwh", "solar_kwh", "saved_p", "unpriced_kwh")


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

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self):
        with self.lock:
            keep = sorted(self.days)[-KEEP_DAYS:]
            data = {k: self.days[k] for k in keep}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=0), encoding="utf-8")
        tmp.replace(self.path)  # never leaves a half-written file
        self._last_save = time.time()

    def add(self, reading, price_p, cheap):
        """Count one inverter reading. price_p None = Octopus not known yet (counted, not priced)."""
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
        key = time.strftime("%Y-%m-%d", time.localtime(t))
        with self.lock:
            if key not in self.days:
                lt = time.localtime(t)
                self.days[key] = {f: 0.0 for f in FIELDS}
                # started counting after midnight: today's totals are only part of the day
                self.days[key]["late_start"] = lt.tm_hour * 60 + lt.tm_min > 5
            d = self.days[key]
            for f in FIELDS:
                d.setdefault(f, 0.0)
            d["house_kwh"] += house
            d["solar_kwh"] += solar
            d["export_kwh"] += exp
            d["export_p"] += exp * self.export_p
            if price_p is None:
                d["unpriced_kwh"] += imp
            else:
                band = "cheap" if cheap else "peak"
                d[f"import_{band}_kwh"] += imp
                d[f"import_{band}_p"] += imp * price_p
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
                "partial": d.get("unpriced_kwh", 0) > 0.05 or bool(d.get("late_start"))}

    def summary(self):
        today = date.today()
        with self.lock:
            days = [self._day((today - timedelta(days=i)).isoformat()) for i in range(14)]

            def total(start):
                ks = [k for k in self.days if k >= start.isoformat()]
                rows = [self._day(k) for k in ks]
                return {"spent_p": round(sum(r["spent_p"] for r in rows), 1),
                        "cheap_p": round(sum(r["cheap_p"] for r in rows), 1),
                        "peak_p": round(sum(r["peak_p"] for r in rows), 1),
                        "saved_p": round(sum(r["saved_p"] for r in rows), 1),
                        "days": len(rows)}

            week = total(today - timedelta(days=today.weekday()))
            month = total(today.replace(day=1))
        return {"today": days[0], "days": [d for d in days if d], "week": week, "month": month}
