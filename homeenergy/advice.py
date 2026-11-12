"""Is now a good time to run the oven, tumble dryer, dishwasher or washing machine?

  GREEN  cheap rate now (night, or an extra car slot while the car charges), or sunshine that's
         truly spare. Solar goes house first, then battery, then the iBoost (hot water) once the
         battery is nearly full. Using sun that would have filled the battery just moves the cost
         to the evening at full price, so it only counts as spare when the battery is nearly full
         or the rest of today's forecast will fill it anyway.
  AMBER  full price, but the battery has plenty to cover it
  RED    full price and little in the battery: wait if you can
Plus when the next cheap time is.
"""
from datetime import datetime, timedelta, timezone

from .solar import sunny_window

SUNNY_W = 1500            # roughly a dishwasher or washing machine heating
BATTERY_OK_PCT = 50
BATTERY_FULLISH_PCT = 90
BATTERY_KWH = 8.2
HOUSE_BASE_KW = 0.4       # rough background use while the sun's up


def _t(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _hm(dt):
    return dt.astimezone().strftime("%H:%M")


def _when(dt, now):
    local, today = dt.astimezone(), now.astimezone().date()
    if local.date() == today:
        return _hm(dt)
    if (local.date() - today).days == 1:
        return "tomorrow " + _hm(dt)
    return local.strftime("%a %H:%M")


def advise(snap, now=None):
    now = now or datetime.now(timezone.utc)
    inv = snap.get("latest")
    cheap = snap.get("cheap") or {}
    rates = (snap.get("rates") or {}).get("rates") or []
    planned = (snap.get("dispatches") or {}).get("planned") or []
    if not inv:
        return {"colour": "grey", "title": "Waiting for readings", "detail": "", "next": ""}

    # When is the next cheap time?
    low = min((r["p"] for r in rates), default=None)
    current_night = next((r for r in rates if low is not None and r["p"] <= low + 0.01
                          and _t(r["from"]) <= now and (r["to"] is None or now < _t(r["to"]))), None)
    next_night = next((r for r in rates if low is not None and r["p"] <= low + 0.01
                       and _t(r["from"]) > now), None)
    # Only the parts of car slots outside the night window matter (the night is cheap anyway)
    next_extra = next((p for p in (snap.get("extras") or []) if _t(p["start"]) > now), None)
    nxt = []
    if next_night:
        nxt.append(f"cheap from {_when(_t(next_night['from']), now)}")
    if next_extra and (not next_night or _t(next_extra["start"]) < _t(next_night["from"])):
        nxt.append(f"maybe {_when(_t(next_extra['start']), now)}-{_hm(_t(next_extra['end']))} "
                   f"if the car is charging")
    next_text = "Next: " + ", or ".join(nxt) if nxt else ""

    # Next sunny spell from the forecast (later today, else tomorrow)
    fc = snap.get("solar_forecast")
    local = now.astimezone()
    sun_text = ""
    for label, day in (("today", local.date()), ("tomorrow", local.date() + timedelta(days=1))):
        win = sunny_window(fc, day.isoformat())
        if win and (label == "tomorrow" or win[1] > local.hour + 1):
            start = max(win[0], local.hour + 1) if label == "today" else win[0]
            sun_text = f"Sunny {label} {start:02d}:00-{win[1]:02d}:00 (up to {win[2] / 1000:.1f} kW)"
            break
    if sun_text:
        next_text = (next_text + ". " if next_text else "") + sun_text

    price = cheap.get("price_p")
    # Is the sunshine spare? (Battery nearly full, or the rest of today will fill it anyway.)
    remaining_kwh, sun_hours = 0.0, 0
    for k, w in ((fc or {}).get("hours") or {}).items():
        if k.startswith(local.date().isoformat()) and int(k[11:13]) > local.hour and w > 0:
            remaining_kwh += w / 1000
            sun_hours += 1
    space_kwh = BATTERY_KWH * (100 - inv["battery_pct"]) / 100
    will_fill = remaining_kwh - HOUSE_BASE_KW * sun_hours > space_kwh + 1.0
    sunny = inv["solar_w"] >= SUNNY_W and inv.get("grid_w", 0) <= 100 and         (inv["battery_pct"] >= BATTERY_FULLISH_PCT or will_fill)
    if cheap.get("now"):
        if current_night:
            detail = f"Night rate ({price}p) until {_hm(_t(current_night['to']))}" if current_night["to"] else "Night rate"
        else:
            detail = f"Cheap ({price}p) while the car is charging"
        return {"colour": "green", "title": "Good time - it's cheap now", "detail": detail, "next": ""}
    if sunny:
        return {"colour": "green", "title": "Good time - the sun's out",
                "detail": f"{inv['solar_w'] / 1000:.1f} kW of solar and the battery " +
                          ("is nearly full" if inv["battery_pct"] >= BATTERY_FULLISH_PCT else "will fill anyway") +
                          " - otherwise it just heats the water",
                "next": next_text}
    if inv["battery_pct"] >= BATTERY_OK_PCT:
        return {"colour": "amber", "title": "OK - the battery can cover it",
                "detail": f"Battery {inv['battery_pct']}%. A long run (oven, dryer) may still use some "
                          f"full-price power" + (f" at {price}p" if price else ""),
                "next": next_text}
    return {"colour": "red", "title": "Wait if you can",
            "detail": f"Full price" + (f" ({price}p)" if price else "") + f" and battery {inv['battery_pct']}%",
            "next": next_text}
