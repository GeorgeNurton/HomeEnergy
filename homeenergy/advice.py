"""Is now a good time to run the oven, tumble dryer, dishwasher or washing machine?

  GREEN  cheap rate now (night, or an extra car slot while the car charges), or spare solar
  AMBER  full price, but the battery has plenty to cover it
  RED    full price and little in the battery: wait if you can
Plus when the next cheap time is.
"""
from datetime import datetime, timezone

SPARE_SOLAR_W = 1500      # roughly a dishwasher or washing machine heating
BATTERY_OK_PCT = 50


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
    next_extra = next((p for p in planned if p.get("extra") and _t(p["start"]) > now), None)
    nxt = []
    if next_night:
        nxt.append(f"cheap from {_when(_t(next_night['from']), now)}")
    if next_extra and (not next_night or _t(next_extra["start"]) < _t(next_night["from"])):
        nxt.append(f"maybe {_when(_t(next_extra['start']), now)}-{_hm(_t(next_extra['end']))} "
                   f"if the car charges then")
    next_text = "Next: " + ", or ".join(nxt) if nxt else ""

    price = cheap.get("price_p")
    spare = inv["solar_w"] - inv["house_w"]
    if cheap.get("now"):
        if current_night:
            detail = f"Night rate ({price}p) until {_hm(_t(current_night['to']))}" if current_night["to"] else "Night rate"
        else:
            detail = f"Cheap ({price}p) while the car is charging"
        return {"colour": "green", "title": "Good time - it's cheap now", "detail": detail, "next": ""}
    if spare >= SPARE_SOLAR_W:
        return {"colour": "green", "title": "Good time - spare sunshine",
                "detail": f"{spare / 1000:.1f} kW of solar going spare", "next": next_text}
    if inv["battery_pct"] >= BATTERY_OK_PCT:
        return {"colour": "amber", "title": "OK - the battery can cover it",
                "detail": f"Battery {inv['battery_pct']}%. A long run (oven, dryer) may still use some "
                          f"full-price power" + (f" at {price}p" if price else ""),
                "next": next_text}
    return {"colour": "red", "title": "Wait if you can",
            "detail": f"Full price" + (f" ({price}p)" if price else "") + f" and battery {inv['battery_pct']}%",
            "next": next_text}
