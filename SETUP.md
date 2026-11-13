# Setting up HomeEnergy in your house

A Raspberry Pi that reads your GivEnergy inverter directly over WiFi, shows a phone dashboard, and
stops the battery draining into the car during Intelligent Octopus Go's extra daytime slots (and
optionally charges it from the grid at the cheap rate instead). About an hour, most of it waiting.

**You need:** GivEnergy inverter + battery with its WiFi dongle on your network, Intelligent Octopus
Go (classic or Go Max), a Raspberry Pi (a Zero 2 W is plenty) with a decent SD card and power supply.
An Ohme charger is useful but not required.

## 1. The Pi
1. In **Raspberry Pi Imager**: Raspberry Pi OS **Lite (64-bit)**. In its settings (the cog): hostname
   `energy`, a username and password, your home WiFi, and on the Services tab **Enable SSH**.
2. Boot it, find its address in your router's device list, and set the router to **always give the
   Pi and the GivEnergy dongle the same address** (the dongle shows up named like `WF2136G384`).
3. From a computer: `ssh <user>@<pi address>`.

## 2. The code
```sh
sudo apt install -y git
git clone https://github.com/GeorgeNurton/HomeEnergy.git homeenergy
cd homeenergy
sh deploy/install.sh
```
No other software needed: it's plain Python. To update later: `git pull` then
`sudo systemctl restart homeenergy`.

## 3. Settings
`nano config.toml` - at least `[givenergy] host` (the dongle's address). The rest is explained in
the file. Leave `[control] live = false` for now.

`nano secrets.toml` (only readable by you, never uploaded anywhere):
- **Octopus API key and account number**: octopus.energy → My account → Personal details → API
  access. The key starts `sk_live_` and is 40 characters; copy it whole.
- **Ohme** email and password, if you have one. Without it, "is the car charging?" is judged from
  the house load (anything over 1.4 kW during a slot), see `[car]` in config.toml.

## 4. Check-up (read-only)
```sh
python3 -m homeenergy.check
```
It changes nothing. It reports your inverter model and battery, its settings and clock, your tariff
and its cheap night band, the car slots Octopus has planned, and anything that needs attention.
Things worth knowing:
- The battery control has been **tested on an AC-coupled inverter**. Hybrid, All-in-One and
  3-phase models should read fine, but watch the control (step 5) before trusting it.
- The inverter needs **eco mode** and **grid charging enabled** with a timed charge for the night
  band. Set `night_slot` in config.toml to match: once live, the program owns charge slot 1.

## 5. Start it, watch, then go live
```sh
sudo systemctl restart homeenergy
```
- Dashboard: `http://<pi address>` · wall display: `/wall` · event log: `/log`.
- **Phones**: Android Chrome needs Settings → Apps → Chrome → Permissions → **Local network** (or
  Nearby devices) → Allow, or the page silently won't load. iPhone: use Safari, or allow Local
  Network for other browsers. Type the `http://` in full.
- With `live = false` the log shows what it *would* do at each extra slot ("now hold (dry run)").
  When that looks right: `live = true` (and `grid_charge = true` if you want the battery filled
  from the grid in extra slots), then `sudo systemctl restart homeenergy`.

## What it does to the inverter (only when live)
Only during an **extra** slot (outside the night band) **and** while the car is actually charging:
- battery discharge limit set to 0% so the battery isn't drained into the car;
- with `grid_charge`: charge slot 1 borrowed from now to the slot's end, so the battery charges at
  the cheap rate too.

Otherwise the discharge limit is put back and slot 1 returned to `night_slot`. It re-checks every
30 s, so a missed change heals itself, and it puts things back on restart. If the Pi dies mid-slot,
fix by hand in a GivEnergy app: charge slot = your night band, discharge rate back to normal.
Logs: `journalctl -u homeenergy -f` and the `/log` page.

## Always-on display
An old phone on a charger with `/wall` open: add it to the home screen, and turn on Developer
options → **Stay awake** (tap Build number 7 times in About phone to get Developer options).
