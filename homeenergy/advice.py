"""Is now a good time to run the oven, tumble dryer, dishwasher or washing machine?

  GREEN  cheap rate now (night, or an extra car slot while the car charges), or sunshine that's
         truly spare. Solar goes house first, then battery, then the iBoost (hot water) once the
         battery is nearly full. Using sun that would have filled the battery just moves the cost
         to the evening at full price, so it only counts as spare when the battery is nearly full
         or the rest of today's forecast will fill it anyway.
  AMBER  full price, but the battery (plus the sun still forecast today) will last until the
         cheap rate even after an appliance run on top of the background use
  RED    otherwise - even a nearly full battery on a dull morning only just covers the house
         until the night rate, so an appliance now means buying full-price power later
Plus when the next cheap time is.
"""
from datetime import datetime, timedelta, timezone

from .solar import sunny_window

SUNNY_W = 1500            # roughly a dishwasher or washing machine heating
BATTERY_OK_PCT = 50
BATTERY_FULLISH_PCT = 90
DEFAULT_BATTERY_KWH = 8.0   # only if neither config nor inverter says
HOUSE_BASE_KW = 0.4       # rough background use while the sun's up
WAIT_BASE_KW = 0.5        # background use until the cheap rate (evenings are a bit higher)
APPLIANCE_KWH = 2.0       # a typical dishwasher / washing machine / oven run


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
        nxt.append(f"Cheap from {_when(_t(next_night['from']), now)}")
    if next_extra and (not next_night or _t(next_extra["start"]) < _t(next_night["from"])):
        nxt.append(f"or {_when(_t(next_extra['start']), now)}-{_hm(_t(next_extra['end']))} "
                   f"if the car charges")
    next_text = " ".join(nxt)

    # Next sunny spell from the forecast (later today, else tomorrow)
    fc = snap.get("solar_forecast")
    local = now.astimezone()
    sun_text = ""
    for label, day in (("today", local.date()), ("tomorrow", local.date() + timedelta(days=1))):
        win = sunny_window(fc, day.isoformat())
        if win and (label == "tomorrow" or win[1] > local.hour + 1):
            start = max(win[0], local.hour + 1) if label == "today" else win[0]
            sun_text = f"Sun {label} {start:02d}-{win[1]:02d}"
            break
    if sun_text:
        next_text = (next_text + " · " if next_text else "") + sun_text

    price = cheap.get("price_p")
    # Is the sunshine spare? (Battery nearly full, or the rest of today will fill it anyway.)
    remaining_kwh, sun_hours = 0.0, 0
    for k, w in ((fc or {}).get("hours") or {}).items():
        if k.startswith(local.date().isoformat()) and int(k[11:13]) > local.hour and w > 0:
            remaining_kwh += w / 1000
            sun_hours += 1
    space_kwh = (snap.get("battery_kwh") or DEFAULT_BATTERY_KWH) * (100 - inv["battery_pct"]) / 100
    will_fill = remaining_kwh - HOUSE_BASE_KW * sun_hours > space_kwh + 1.0
    sunny = inv["solar_w"] >= SUNNY_W and inv.get("grid_w", 0) <= 100 and         (inv["battery_pct"] >= BATTERY_FULLISH_PCT or will_fill)
    # Will the battery (plus the rest of today's sun) last until the cheap rate with an
    # appliance run on top of the background use?
    kwh = snap.get("battery_kwh") or DEFAULT_BATTERY_KWH
    reserve = (inv.get("settings") or {}).get("reserve_pct") or 4
    lasts = None
    if next_night:
        hours = (_t(next_night["from"]) - now).total_seconds() / 3600
        usable = kwh * (100 - reserve) / 100
        have = min(kwh * max(inv["battery_pct"] - reserve, 0) / 100 + remaining_kwh,
                   usable + HOUSE_BASE_KW * sun_hours)     # the battery can't hold more than full
        lasts = have >= WAIT_BASE_KW * hours + APPLIANCE_KWH
    else:
        lasts = inv["battery_pct"] >= BATTERY_OK_PCT          # no tariff yet: rough guess
    until = _hm(_t(next_night["from"])) if next_night else ""

    if cheap.get("now"):
        if current_night and current_night["to"]:
            detail = f"Cheap rate till {_hm(_t(current_night['to']))}"
        elif current_night:
            detail = "Cheap rate"
        else:
            detail = "Cheap while the car charges"
        return {"colour": "green", "title": "GOOD", "detail": detail, "next": ""}
    if sunny:
        return {"colour": "green", "title": "GOOD",
                "detail": f"Spare sun · {inv['solar_w'] / 1000:.1f} kW", "next": next_text}
    if lasts:
        return {"colour": "amber", "title": "OK",
                "detail": f"Battery lasts till {until}" if until else "Battery can cover it",
                "next": next_text}
    return {"colour": "red", "title": "WAIT",
            "detail": f"Battery won't last till {until}" if until else
                      "Full price" + (f" · {price}p" if price else ""),
            "next": next_text}
