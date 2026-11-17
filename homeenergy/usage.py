"""Hour-by-hour house use, with the car taken out, to learn the household's normal pattern.

For each day it keeps 24 hourly totals (kWh) of:
  home   - the house without the car (house load minus what the Ohme says it's drawing)
  car    - what the Ohme drew
  solar  - what the panels made
  secs   - how many seconds of each hour were actually seen (gaps when the Pi was off)

The "typical" day is the median of recent days, hour by hour, for the same weekday where there
are enough of them (Wednesdays and Thursdays differ), otherwise all days. Medians, not averages,
so an occasional big one-off (composites curing in the shed) doesn't skew the normal pattern.

It is the groundwork for setting the night charge from tomorrow's solar forecast.
"""
import json
import statistics
import threading
import time
from datetime import date, timedelta
from pathlib import Path

KEEP_DAYS = 800
MAX_GAP_S = 120            # longer gaps between readings aren't counted (Pi off, inverter silent)
FULL_HOUR_S = 3000         # an hour counts towards the typical day if at least 50 min was seen
SAME_WEEKDAY_MIN = 4       # this many of the same weekday before using a weekday-only pattern
LOOKBACK_DAYS = 56         # the typical day is drawn from the last 8 weeks
KINDS = ("home", "car", "solar", "secs")


class UsageLog:
    def __init__(self, path):
        self.path = Path(path)
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
            data = {k: self.days[k] for k in sorted(self.days)[-KEEP_DAYS:]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)     # never leaves a half-written file
        self._last_save = time.time()

    def add(self, reading, car_w=0):
        """Count one inverter reading. car_w = what the car charger is drawing right now."""
        t = reading["time"]
        last, self._last_time = self._last_time, t
        if last is None or not 0 < t - last <= MAX_GAP_S:
            return
        secs = t - last
        h = secs / 3600
        lt = time.localtime(t)
        key, hour = time.strftime("%Y-%m-%d", lt), lt.tm_hour
        house_w = max(reading.get("house_w", 0), 0)
        car_w = min(max(car_w or 0, 0), house_w)
        with self.lock:
            d = self.days.setdefault(key, {k: [0.0] * 24 for k in KINDS})
            d["home"][hour] += (house_w - car_w) * h / 1000
            d["car"][hour] += car_w * h / 1000
            d["solar"][hour] += max(reading.get("solar_w", 0), 0) * h / 1000
            d["secs"][hour] += secs
        if time.time() - self._last_save > 300:
            self.save()

    def typical(self, day):
        """Median home use for each hour of a day like `day` (a date). Returns (24 kWh or None, basis)."""
        start = (day - timedelta(days=LOOKBACK_DAYS)).isoformat()
        with self.lock:
            past = {k: v for k, v in self.days.items() if start <= k < day.isoformat()}
        same = {k: v for k, v in past.items() if date.fromisoformat(k).weekday() == day.weekday()}
        use, basis = (same, day.strftime("%A")) if len(same) >= SAME_WEEKDAY_MIN else (past, "all days")
        hours, counts = [], []
        for hr in range(24):
            vals = [d["home"][hr] * 3600 / d["secs"][hr] for d in use.values() if d["secs"][hr] >= FULL_HOUR_S]
            counts.append(len(vals))
            hours.append(round(statistics.median(vals), 2) if vals else None)
        return hours, {"basis": basis, "days": max(counts) if counts else 0}

    def summary(self):
        today = date.today()
        with self.lock:
            d = self.days.get(today.isoformat())
            d = {k: list(v) for k, v in d.items()} if d else None
        typ, info = self.typical(today)
        return {
            "today": [round(x, 2) for x in d["home"]] if d else [0.0] * 24,
            "today_car_kwh": round(sum(d["car"]), 1) if d else 0.0,
            "today_home_kwh": round(sum(d["home"]), 1) if d else 0.0,
            "typical": typ,
            "typical_kwh": round(sum(x for x in typ if x is not None), 1) if any(x is not None for x in typ) else None,
            **info,
        }
