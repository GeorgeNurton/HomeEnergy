# HomeEnergy

A Raspberry Pi Zero 2 W that watches the house's electricity and steers the GivEnergy battery
around Intelligent Octopus Go's cheap slots. **Setting it up in another house: see [SETUP.md](SETUP.md).**

## The kit (the original house)
- GivEnergy AC-coupled inverter (CE2146G206) + Giv-Bat 8.2 (8.2 kWh), WiFi dongle at `192.168.1.65`
- Separate solar inverter, Marlec iBoost (left alone)
- Ohme Home Pro (Citroen eC3), granny charger (Mitsubishi PHEV)
- Octopus Intelligent Go

## How it works
- Talks to the inverter **directly over WiFi** (dongle port 8899, GivEnergy's Modbus wrapper). No cloud.
- Polls every 10 s, keeps 24 h of one-minute history in memory (nothing written to the SD card).
- Serves a phone dashboard (`/`), a wall display (`/wall`) and an event log (`/log`).
- Python standard library only; nothing to install.

## Run it
```
python -m homeenergy           # the dashboard and battery control
python -m homeenergy.check     # read-only check-up of the inverter, Octopus, Ohme and forecast
```
Settings live in `config.toml` (from `config.example.toml`), keys in `secrets.toml` (from
`secrets.example.toml`). Neither is put on GitHub.

## Plan
1. ✅ Read-only dashboard (inverter)
2. ✅ Octopus (Intelligent Octopus Go Max prices, car smart slots) and Ohme (is the car actually charging?)
3. ✅ Battery control (`controller.py`), **LIVE** since 2026-11-12 (`[control]` in config.toml):
   - Extra car slot (reaching outside the 23:30-05:30 night window) AND the car actually charging:
     - HOLD: battery discharge limit (HR 112) 0%, so it doesn't drain into the car.
     - CHARGE (`grid_charge`): borrow charge slot 1 (HR 94/95) from now to the slot's end, so the
       battery fills from the grid at the cheap rate. (Slot 2, HR 31/32, is refused by this inverter.)
   - Otherwise NORMAL: discharge limit restored, slot 1 back to `night_slot` (23:30-05:30).
   - Settings are re-checked every 30 s (self-healing); a crash mid-slot is undone on restart.
   - If it dies mid-charge, the night charge is missed until restart: set the charge slot back to
     23:30-05:30 and discharge rate to 50% in the phone app.
4. ✅ Event log (`logs/events.jsonl`, page at `/log`) and red alert banner on the dashboard for
   refused/failed inverter changes and any source failing for 5+ minutes.
5. ✅ Wall display for an always-on phone (`/wall`)
6. ✅ Shareable: settings template, check-up script, Ohme optional ([SETUP.md](SETUP.md))
7. Extras: e-ink screen, PHEV smart plug, supply-current limit (100 A main fuse)

## Register notes (checked against the real inverter, Nov 2026)
| Reg | Meaning |
|---|---|
| IR 18 | solar W (PV clamp) |
| IR 30 | grid W, + export / - import |
| IR 42 | house load W |
| IR 52 | battery W, + discharging / - charging |
| IR 59 | battery % |
| IR 25 / 26 | export / import today, kWh x10 |
| HR 27 | battery mode (1 = eco) |
| HR 31-32 | charge slot 2 (hhmm) |
| HR 94-95 | charge slot 1 (hhmm) |
| HR 96 | grid charging enabled |
| HR 110 | reserve % |
| HR 116 | charge target % |
| HR 35-40 | inverter clock |
