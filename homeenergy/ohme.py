"""Ohme charger: is the car plugged in, and is it actually charging right now?

Uses the same (unofficial) API as the Ohme app. Read-only. Standard library only.
"""
import json
import time
import urllib.parse
import urllib.request

# Ohme app's public Firebase key (the same one the app and the ohmepy library use)
GOOGLE_KEY = "AIzaSyC8ZeZngm33tpOXLpbXeKfwtyZ1WrkbdBY"
API = "https://api.ohme.io"
UA = "homeenergy/0.1"


class Ohme:
    def __init__(self, email, password, timeout=20):
        self.email = email
        self.password = password
        self.timeout = timeout
        self._token = None
        self._refresh = None
        self._token_at = 0

    def _post_form(self, url, fields):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def _get_token(self):
        age = time.time() - self._token_at
        if self._token and age < 45 * 60:
            return self._token
        if self._refresh:
            try:
                r = self._post_form(f"https://securetoken.googleapis.com/v1/token?key={GOOGLE_KEY}",
                                    {"grantType": "refresh_token", "refreshToken": self._refresh})
                self._token, self._refresh = r["id_token"], r["refresh_token"]
                self._token_at = time.time()
                return self._token
            except Exception:
                pass  # fall back to a full login
        r = self._post_form(
            f"https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key={GOOGLE_KEY}",
            {"email": self.email, "password": self.password, "returnSecureToken": "true"})
        self._token, self._refresh = r["idToken"], r["refreshToken"]
        self._token_at = time.time()
        return self._token

    def _get(self, path):
        req = urllib.request.Request(API + path, headers={
            "Authorization": f"Firebase {self._get_token()}",
            "Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def status(self):
        sessions = self._get("/v1/chargeSessions")
        s = sessions[0] if sessions else {}
        power = s.get("power") or {}
        mode = s.get("mode") or "UNKNOWN"
        soc = ((s.get("car") or {}).get("batterySoc") or s.get("batterySoc") or {}).get("percent")
        watts = power.get("watt") or 0
        return {
            "time": time.time(),
            "mode": mode,
            "plugged_in": mode != "DISCONNECTED",
            "charging": watts > 0,
            "power_w": watts,
            "amps": power.get("amp"),
            "car_pct": soc,
            "online": (s.get("chargerStatus") or {}).get("online"),
        }
