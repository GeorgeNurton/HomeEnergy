"""Solar forecast from forecast.solar (free, no account, 12 lookups an hour per address).

Only the postcode *district* centre is sent (rounded to ~1 km), with the panels' tilt, direction
and size. Hourly watts come back in UK local time; partial shade early and late is allowed for
by scaling those hours down.
"""
import json
import time
import urllib.request
from datetime import datetime

UA = "homeenergy/0.1"


class SolarForecast:
    def __init__(self, lat, lon, tilt, azimuth, kwp, shade_before=8, shade_after=17, shade_factor=0.5,
                 timeout=20):
        # forecast.solar azimuth: 0 = south, -90 = east, 90 = west
        self.url = f"https://api.forecast.solar/estimate/{lat:.2f}/{lon:.2f}/{tilt}/{azimuth}/{kwp}"
        self.shade_before, self.shade_after, self.shade_factor = shade_before, shade_after, shade_factor
        self.timeout = timeout

    def fetch(self):
        req = urllib.request.Request(self.url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = json.load(r)
        hours = {}
        for ts, w in body["result"]["watts"].items():
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            if t.hour < self.shade_before or t.hour >= self.shade_after:
                w *= self.shade_factor
            key = t.strftime("%Y-%m-%d %H:00")
            hours[key] = max(hours.get(key, 0), round(w))
        days = {}
        for key, w in hours.items():
            days[key[:10]] = days.get(key[:10], 0) + w / 1000   # ~kWh, one value per hour
        return {"time": time.time(), "hours": hours,
                "days": {k: round(v, 1) for k, v in sorted(days.items())}}


def sunny_window(forecast, day, min_w=1200):
    """Longest run of hours on `day` (YYYY-MM-DD) forecast at or above min_w.
    Returns (start_hour, end_hour, peak_w) or None."""
    if not forecast:
        return None
    hrs = sorted((int(k[11:13]), w) for k, w in forecast["hours"].items() if k.startswith(day))
    best, run = None, []
    for h, w in hrs + [(99, 0)]:
        if w >= min_w and (not run or h == run[-1][0] + 1):
            run.append((h, w))
            continue
        if run and (not best or len(run) > len(best)):
            best = run
        run = [(h, w)] if w >= min_w else []
    if not best:
        return None
    return best[0][0], best[-1][0] + 1, max(w for _, w in best)
