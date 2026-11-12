# HomeEnergy

A Raspberry Pi Zero 2 W that watches the house's electricity and (soon) steers the
GivEnergy battery around Intelligent Octopus Go's cheap slots.

## The kit
- GivEnergy AC-coupled inverter (CE2146G206) + Giv-Bat 8.2 (8.2 kWh), WiFi dongle at `192.168.1.65`
- Separate solar inverter, Marlec iBoost (left alone)
- Ohme Home Pro (Citroen eC3), granny charger (Mitsubishi PHEV)
- Octopus Intelligent Go

## How it works
- Talks to the inverter **directly over WiFi** (dongle port 8899, GivEnergy's Modbus wrapper). No cloud.
- Polls every 10 s, keeps 24 h of one-minute history in memory (nothing written to the SD card).
- Serves a phone dashboard: `http://energy.local` on the Pi, `http://localhost:8080` when testing on a PC.
- Python standard library only; nothing to install.

## Run it
```
python -m homeenergy
```
Settings live in `config.toml`. API keys (Octopus, Ohme) will go in `secrets.toml`, which git ignores.

## Plan
1. ✅ Read-only dashboard (inverter)
2. ✅ Octopus (Intelligent Octopus Go Max prices, car smart slots) and Ohme (is the car actually charging?)
3. ✅ Battery control (`controller.py`), **LIVE** since 2026-11-12 (`[control] live` in config.toml):
   - Car slot reaching outside the 23:30-05:30 night window ("extra") AND the car actually charging
     -> HOLD: battery discharge limit (HR 112) set to 0%, restored afterwards (normally 50%).
   - Night is left to the inverter: its own grid charge now runs 23:30-05:30.
   - Missing or stale data means NORMAL. The setting is re-checked every 30 s (self-healing), and a
     limit found stuck at 0% on startup is restored.
   - If it dies mid-hold the battery stays paused: restart it, or set the discharge rate back in the app.
   - Next: optionally grid-charge the battery during extra slots too.
4. Extras: small screen, PHEV smart plug, supply-current limit (100 A main fuse)

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
