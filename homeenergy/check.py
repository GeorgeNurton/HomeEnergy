"""Check-up: run this first on a new house. It only READS - nothing is changed anywhere.

    python3 -m homeenergy.check

Reports what inverter and battery you have, how they're set up, whether Octopus and the Ohme
answer, and whether the battery control is likely to work. Best run while the HomeEnergy service
is stopped (the inverter's dongle only talks to one program at a time).
"""
import time
import tomllib
from datetime import datetime, timezone

from .givenergy import TESTED_MODELS, Inverter
from .octopus import Octopus
from .ohme import Ohme
from .server import ROOT, _filled, extra_times
from .solar import SolarForecast

results = []


def say(level, text):
    mark = {"ok": "  OK  ", "warn": " WARN ", "fail": " FAIL ", "info": "      "}[level]
    print(f"[{mark}] {text}")
    if level in ("warn", "fail"):
        results.append((level, text))


def _hhmm(v):
    return f"{v // 100:02d}:{v % 100:02d}"


def check_inverter(cfg):
    print("\n== GivEnergy inverter ==")
    host = cfg.get("givenergy", {}).get("host", "")
    if not host or "XX" in host:
        say("fail", "set [givenergy] host in config.toml to the dongle's address")
        return None
    inv = Inverter(host)
    st = None
    for attempt in range(4):
        try:
            st = inv.status()
            break
        except Exception as e:
            if attempt == 3:
                say("fail", f"no answer from {host}:8899 ({type(e).__name__}: {e}). Is the address right, "
                            f"and is the HomeEnergy service (or another GivEnergy app) stopped?")
                return None
            time.sleep(3)
    s = st["settings"]
    say("ok", f"talking to the inverter at {host}")
    say("info", f"model: {st['model']} (code {st['device_code']}), firmware {st['firmware']}, "
                f"serial {st['inverter_serial']}, battery serial {st['battery_serial']}")
    kwh = st["battery_ah"] * 51.2 / 1000 if st["battery_ah"] else None
    say("info", f"battery: {st['battery_ah']} Ah (~{kwh:.1f} kWh), now {st['battery_pct']}%, "
                f"{st['battery_v']} V, {st['battery_temp_c']} C" if kwh else
                f"battery size not reported - set [house] battery_kwh in config.toml")
    say("info", f"right now: solar {st['solar_w']} W, house {st['house_w']} W, grid {st['grid_w']} W "
                f"(+ = importing), battery {st['battery_w']} W (+ = discharging)")
    say("info", f"settings: {s['mode']}, grid charging {'on' if s['grid_charge_enabled'] else 'off'}, "
                f"charge slot 1 {s['charge_slot_1']}, slot 2 {s['charge_slot_2']}, target {s['charge_target_pct']}%, "
                f"reserve {s['reserve_pct']}%, charge/discharge limits {s['charge_limit']}%/{s['discharge_limit']}%")

    if st["model"] in TESTED_MODELS:
        say("ok", f"{st['model']} inverters are the type the battery control was tested on")
    else:
        say("warn", f"{st['model']} inverter: the battery control has only been tested on AC-coupled. "
                    f"Readings should be fine; keep [control] live = false and watch the /log page "
                    f"before switching it on")
    if s["mode"] != "eco":
        say("warn", f"battery mode is '{s['mode']}', not eco: the battery control assumes eco mode")
    if not s["grid_charge_enabled"]:
        say("warn", "grid charging is off on the inverter: the night charge and grid_charge won't work "
                    "until it's switched on in the inverter's settings")
    if s["discharge_limit"] == 0:
        say("warn", "battery discharge limit is 0% (battery paused) - is something else controlling it?")
    night = str(cfg.get("control", {}).get("night_slot", "2330-0530")).split("-")
    want = (int(night[0]), int(night[1]))
    if (s["slot1_start"], s["slot1_end"]) != want:
        say("info", f"charge slot 1 is {s['charge_slot_1']}; with live = true the program will set it to "
                    f"{_hhmm(want[0])}-{_hhmm(want[1])} (night_slot in config.toml)")
    try:
        clock = datetime.strptime(st["inverter_clock"], "%Y-%m-%d %H:%M")
        drift = abs((clock - datetime.now()).total_seconds()) / 60
        if drift > 5:
            say("warn", f"inverter clock is {st['inverter_clock']}, {drift:.0f} min out: timed charging "
                        f"will be off by that much (fix the clock in the inverter's settings)")
        else:
            say("ok", f"inverter clock right ({st['inverter_clock']})")
    except ValueError:
        say("warn", f"couldn't read the inverter clock ({st['inverter_clock']})")
    return st


def check_octopus(secrets):
    print("\n== Octopus ==")
    octo = secrets.get("octopus")
    if not _filled(octo, "api_key", "account"):
        say("fail", "no Octopus api_key / account in secrets.toml: no prices or car slots without them")
        return
    o = Octopus(octo["api_key"], octo["account"])
    try:
        r = o.rates()
    except Exception as e:
        say("fail", f"couldn't get the tariff ({type(e).__name__}: {e}). Check the API key (40 characters, "
                    f"sk_live_...) and account number")
        return
    rates = r["rates"]
    low = min(x["p"] for x in rates)
    night = next((x for x in rates if x["p"] <= low + 0.01 and x["to"]), None)
    say("ok", f"tariff {r['tariff']} ({r['name']}), {len(rates)} price bands, cheapest {low}p")
    if night:
        f = datetime.fromisoformat(night["from"].replace("Z", "+00:00")).astimezone()
        t = datetime.fromisoformat(night["to"].replace("Z", "+00:00")).astimezone()
        say("info", f"cheap night band {f:%H:%M}-{t:%H:%M} (set the inverter's night_slot to match)")
    if "INTELLI" not in r["tariff"].upper() and "IOG" not in r["tariff"].upper():
        say("warn", "this doesn't look like an Intelligent Octopus tariff: there won't be extra car slots")
    try:
        d = o.dispatches()
    except Exception as e:
        say("fail", f"couldn't get smart-charge slots ({type(e).__name__}: {e})")
        return
    if not d["devices"]:
        say("warn", "no smart-charging device on the account, so no extra car slots will show up")
    for dev in d["devices"]:
        say("info", f"device: {dev['provider'] or dev['type']} ({dev['state']})")
    extras = extra_times(d["planned"], rates)
    say("ok", f"{len(d['planned'])} car slot(s) planned, {len(extras)} of them outside the night band")


def check_ohme(secrets, cfg):
    print("\n== Car charger ==")
    oh = secrets.get("ohme")
    check = cfg.get("car", {}).get("charging_check", "auto")
    if not _filled(oh, "email", "password"):
        how = "house load" if check in ("auto", "load") else check
        say("info", f"no Ohme login in secrets.toml - 'is the car charging?' will use: {how}")
        return
    try:
        s = Ohme(oh["email"], oh["password"]).status()
        say("ok", f"Ohme: {s['mode'].lower()}, plugged in {s['plugged_in']}, charging {s['charging']}, "
                  f"car {s['car_pct']}%")
    except Exception as e:
        say("fail", f"Ohme login failed ({type(e).__name__}: {e})")


def check_solar(cfg):
    print("\n== Solar forecast ==")
    sol = cfg.get("solar") or {}
    if not sol.get("kwp"):
        say("info", "switched off ([solar] kwp = 0)")
        return
    try:
        fc = SolarForecast(sol["lat"], sol["lon"], sol["tilt"], sol["azimuth"], sol["kwp"]).fetch()
        say("ok", "forecast: " + ", ".join(f"{k} ~{v} kWh" for k, v in fc["days"].items()))
    except Exception as e:
        say("warn", f"forecast.solar didn't answer ({type(e).__name__}: {e}) - it's free and sometimes busy")


def main():
    print("HomeEnergy check-up (read-only: nothing is changed)")
    cfg_file = ROOT / "config.toml"
    if not cfg_file.exists():
        print("No config.toml yet: copy config.example.toml to config.toml and fill it in.")
        return
    cfg = tomllib.loads(cfg_file.read_text())
    sf = ROOT / "secrets.toml"
    secrets = tomllib.loads(sf.read_text()) if sf.exists() else {}
    check_inverter(cfg)
    check_octopus(secrets)
    check_ohme(secrets, cfg)
    check_solar(cfg)
    ctl = cfg.get("control", {})
    print("\n== Summary ==")
    print(f"battery control: {'LIVE' if ctl.get('live') else 'watch only (live = false)'}, "
          f"grid charging in extra slots: {'on' if ctl.get('grid_charge') else 'off'}")
    if not results:
        print("Everything looks fine.")
    for level, text in results:
        print(f"- {level.upper()}: {text}")


if __name__ == "__main__":
    main()
