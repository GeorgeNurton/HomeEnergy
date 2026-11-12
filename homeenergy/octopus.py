"""Octopus Energy: unit rates (REST) and Intelligent Go planned dispatches (GraphQL).

Read-only. Standard library only.
"""
import base64
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.octopus.energy"
GRAPHQL = API + "/v1/graphql/"
UA = "homeenergy/0.1"


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_sunday(year, month):
    d = datetime(year, month + 1, 1) - timedelta(days=1) if month < 12 else datetime(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def uk_local(day, hour, minute):
    """UK wall-clock time on `day` as a UTC datetime (BST = last Sun Mar to last Sun Oct, 01:00 UTC).
    Done by hand so it works on Windows too, which has no timezone database by default."""
    naive = datetime(day.year, day.month, day.day, hour, minute)
    bst_start = _last_sunday(day.year, 3).replace(hour=1)
    bst_end = _last_sunday(day.year, 10).replace(hour=1)
    offset = timedelta(hours=1) if bst_start <= naive - timedelta(hours=1) < bst_end else timedelta(0)
    return (naive - offset).replace(tzinfo=timezone.utc)


class Octopus:
    def __init__(self, api_key, account, timeout=20):
        self.key = api_key.strip()
        self.account = account.strip()
        self.timeout = timeout
        self._token = None
        self._token_at = 0
        self._device_ids = None
        self._devices_at = 0

    # ---- plumbing -------------------------------------------------------
    def _rest(self, path):
        req = urllib.request.Request(API + path, headers={"User-Agent": UA})
        auth = base64.b64encode(f"{self.key}:".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def _gql(self, query, auth=True):
        headers = {"Content-Type": "application/json", "User-Agent": UA}
        if auth:
            headers["Authorization"] = self._get_token()
        req = urllib.request.Request(GRAPHQL, data=json.dumps({"query": query}).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = json.load(r)
        if body.get("errors"):
            raise RuntimeError("; ".join(e.get("message", "?") for e in body["errors"]))
        return body["data"]

    def _get_token(self):
        if not self._token or time.time() - self._token_at > 50 * 60:
            q = 'mutation { obtainKrakenToken(input: {APIKey: "%s"}) { token } }' % self.key
            self._token = self._gql(q, auth=False)["obtainKrakenToken"]["token"]
            self._token_at = time.time()
        return self._token

    # ---- tariff ---------------------------------------------------------
    def tariff_code(self):
        acc = self._rest(f"/v1/accounts/{self.account}/")
        now = datetime.now(timezone.utc)
        for prop in acc.get("properties", []):
            for mp in prop.get("electricity_meter_points", []):
                if mp.get("is_export"):
                    continue
                for ag in mp.get("agreements", []):
                    start, end = _parse(ag.get("valid_from")), _parse(ag.get("valid_to"))
                    if start and start <= now and (end is None or end > now):
                        return ag["tariff_code"]
        raise RuntimeError("no active import tariff found on the account")

    def rates(self):
        """Unit rates from yesterday to the end of tomorrow, oldest first, prices in p/kWh inc VAT."""
        code = self.tariff_code()
        product = code[5:-2]  # E-1R-<product>-A
        start = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%MZ")
        path = (f"/v1/products/{product}/electricity-tariffs/{code}/standard-unit-rates/"
                f"?period_from={start}&page_size=200")
        rows = self._rest(path)["results"]
        if rows:  # classic Intelligent Go: Octopus publishes the time bands itself
            out = [{"from": r["valid_from"], "to": r["valid_to"], "p": round(r["value_inc_vat"], 2)} for r in rows]
            out.sort(key=lambda r: r["from"])
            return {"tariff": code, "name": product, "rates": out}
        return self._four_rate(product, code)

    def _four_rate(self, product, code):
        """Newer 'Go Max' style tariff: house day/night rates plus separate car (EV device) rates.
        Octopus gives the prices but not the times, so the night band is the advertised 23:30-05:30."""
        info = self._rest(f"/v1/products/{product}/")
        tariff = None
        for region in info.get("four_rate_ev_electricity_tariffs", {}).values():
            for t in region.values():
                if t.get("code") == code:
                    tariff = t
        if not tariff:
            raise RuntimeError(f"tariff {code} not found in product {product}")
        day, night = tariff["day_unit_rate_inc_vat"], tariff["night_unit_rate_inc_vat"]
        out = []
        today = datetime.now(timezone.utc).date()
        for offset in (-1, 0, 1):
            d = today + timedelta(days=offset)
            n_start = uk_local(d, 23, 30)
            n_end = uk_local(d + timedelta(days=1), 5, 30)
            next_night = uk_local(d + timedelta(days=1), 23, 30)
            out.append({"from": _iso(n_start), "to": _iso(n_end), "p": round(night, 2)})
            out.append({"from": _iso(n_end), "to": _iso(next_night), "p": round(day, 2)})
        return {"tariff": code, "name": info.get("display_name", product), "rates": out,
                "car_offpeak_p": round(tariff["ev_device_off_peak_unit_rate_inc_vat"], 2),
                "car_peak_p": round(tariff["ev_device_peak_unit_rate_inc_vat"], 2),
                "four_rate": True}

    # ---- intelligent dispatches ----------------------------------------
    def devices(self):
        q = 'query { devices(accountNumber: "%s") { id deviceType provider status { currentState } } }' % self.account
        return self._gql(q)["devices"] or []

    def dispatches(self):
        """Planned smart-charge slots for every Octopus-controlled device."""
        if self._device_ids is None or time.time() - self._devices_at > 3600:
            self._device_ids = [d for d in self.devices()]
            self._devices_at = time.time()
        planned = []
        for d in self._device_ids:
            q = ('query { flexPlannedDispatches(deviceId: "%s") { start end type energyAddedKwh } }' % d["id"])
            for p in self._gql(q)["flexPlannedDispatches"] or []:
                planned.append({"device": d.get("provider") or d.get("deviceType"), "start": p["start"],
                                "end": p["end"], "type": p.get("type"),
                                # Octopus sends this as text, negative (energy flowing into the car)
                                "kwh": abs(float(p["energyAddedKwh"])) if p.get("energyAddedKwh") else None})
        planned.sort(key=lambda p: p["start"])
        return {"devices": [{"id": d["id"], "type": d.get("deviceType"), "provider": d.get("provider"),
                             "state": (d.get("status") or {}).get("currentState")} for d in self._device_ids],
                "planned": planned}


def cheap_now(rates, planned, now=None):
    """True if now is in the cheapest rate band, or inside a planned dispatch."""
    now = now or datetime.now(timezone.utc)
    current = None
    if rates:
        cheapest = min(r["p"] for r in rates)
        for r in rates:
            if _parse(r["from"]) <= now and (r["to"] is None or now < _parse(r["to"])):
                current = r["p"]
        if current is not None and current <= cheapest + 0.01:
            return True, current, "off-peak"
    for p in planned or []:
        if _parse(p["start"]) <= now < _parse(p["end"]):
            # Billed at the off-peak rate during a dispatch
            return True, min(r["p"] for r in rates) if rates else current, "smart charge slot"
    return False, current, None
