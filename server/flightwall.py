#!/usr/bin/env python3
"""FlightWall - a live nearby-aircraft board served from your own machine.

Stdlib only, on purpose. This is meant to sit on an always-on laptop for years
without anyone running `pip install` again.

  python3 server/flightwall.py --port 8730

Then point a phone at it over HTTPS (see scripts/expose.sh). HTTPS is not
optional: browsers refuse `navigator.geolocation` on an insecure origin, and
this whole app is built around the phone's location.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import landmarks

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
STATE = Path(os.environ.get("FLIGHTWALL_HOME", Path.home() / ".flightwall"))

USER_AGENT = "FlightWall/1.0 (+https://github.com/exspo/flightwall)"

# Every one of these speaks the readsb/tar1090 `aircraft.json` dialect, so a
# single parser covers all three and any one of them can be down.
PROVIDERS = [
    {
        "name": "adsb.lol",
        "url": "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}",
    },
    {
        "name": "adsb.fi",
        "url": "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{radius}",
    },
    {
        "name": "airplanes.live",
        "url": "https://api.airplanes.live/v2/point/{lat}/{lon}/{radius}",
    },
]

ROUTE_URL = "https://api.adsb.lol/api/0/routeset"
# Fallback, one callsign per request rather than a batch, so it is only worth
# reaching for when the batch endpoint is unhealthy.
ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
ADSBDB_MAX_LOOKUPS = 20

AIRCRAFT_TTL = 4.0  # seconds; upstreams update about this often
ROUTE_TTL = 60 * 60 * 24 * 30
MAX_RADIUS_NM = 250

# ICAO type codes that are rotorcraft but do not always broadcast category A7.
HELI_TYPES = {
    "A109", "A119", "A139", "A149", "A169", "A189", "AS32", "AS3B", "AS50",
    "AS55", "AS65", "B06", "B06T", "B105", "B117", "B190", "B212", "B222",
    "B230", "B247", "B407", "B412", "B429", "B430", "B47G", "B505", "BK17",
    "EC20", "EC25", "EC30", "EC35", "EC45", "EC55", "EC75", "EH10", "EXPL",
    "GAZL", "H125", "H130", "H135", "H145", "H155", "H160", "H500", "H60",
    "HUCO", "K126", "KA32", "LYNX", "MD52", "MD60", "MI8", "MI17", "MI24",
    "NH90", "PUMA", "R22", "R44", "R66", "S269", "S300", "S330", "S61",
    "S64", "S76", "S92", "SH14", "SK76", "UH1", "UH60", "W3", "WASP",
}

# Categories from the ADS-B emitter-category field.
CAT_LIGHT = {"A1"}
CAT_SMALL = {"A2"}
CAT_LARGE = {"A3", "A4", "A5"}
CAT_ROTOR = {"A7"}
CAT_SPECIAL = {"A6"}
CAT_LIGHTER = {"B1", "B2", "B3", "B4"}
CAT_DRONE = {"B6"}
CAT_GROUND = {"C1", "C2", "C3"}

EMERGENCY_SQUAWKS = {"7500": "HIJACK", "7600": "RADIO FAIL", "7700": "EMERGENCY"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- geo helpers

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3440.065  # nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


# -------------------------------------------------------------------- caching

class TTLCache:
    """Tiny thread-safe TTL cache. Also used to collapse concurrent fetches."""

    def __init__(self, ttl: float, max_entries: int = 4096):
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            expires, value = hit
            if expires < time.time():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key, value, ttl: float | None = None) -> None:
        with self._lock:
            if len(self._data) >= self.max_entries:
                # Cheapest possible eviction: drop whatever expires soonest.
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (time.time() + (self.ttl if ttl is None else ttl), value)

    def load(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        now = time.time()
        with self._lock:
            for key, (expires, value) in raw.items():
                if expires > now:
                    self._data[key] = (expires, value)

    def dump(self, path: Path) -> None:
        with self._lock:
            snapshot = dict(self._data)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot))
            tmp.replace(path)
        except OSError:
            pass


aircraft_cache = TTLCache(AIRCRAFT_TTL)
route_cache = TTLCache(ROUTE_TTL)
# The ground does not move, so one view stays valid for as long as the phone
# stays roughly put.
landmark_cache = TTLCache(60 * 30, max_entries=256)
provider_health: dict = {}
_provider_lock = threading.Lock()


def note_provider(name: str, ok: bool, detail: str = "") -> None:
    with _provider_lock:
        provider_health[name] = {
            "ok": ok,
            "detail": detail,
            "at": round(time.time()),
        }


# ------------------------------------------------------------ upstream access

def fetch_json(url: str, payload: dict | None = None, timeout: float = 8.0):
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = resp.status
        text = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        # "Expecting value: line 1 column 1" says nothing about what actually
        # came back. Quote it, so a 200-with-an-HTML-body is obvious.
        snippet = " ".join(text.split())[:140] or "<empty body>"
        raise ValueError(f"HTTP {status} returned non-JSON: {snippet}") from None


def classify(ac: dict) -> str:
    """Bucket an aircraft so the phone can filter the noise out."""
    cat = (ac.get("category") or "").upper()
    typ = (ac.get("t") or "").upper()
    flight = (ac.get("flight") or "").strip().upper()

    if int(ac.get("dbFlags") or 0) & 1:
        return "military"
    if cat in CAT_ROTOR or typ in HELI_TYPES:
        return "helicopter"
    if cat in CAT_GROUND:
        return "ground"
    if cat in CAT_DRONE:
        return "drone"
    if cat in CAT_LIGHTER:
        return "lighter"
    if cat in CAT_LARGE or cat in CAT_SPECIAL:
        return "commercial"
    # An airline callsign is three letters then digits (UAL1234). A tail number
    # registration (N814GA) is the classic sign of general aviation.
    if len(flight) >= 4 and flight[:3].isalpha() and flight[3].isdigit():
        return "commercial"
    if cat in CAT_LIGHT or cat in CAT_SMALL:
        return "general"
    return "general" if flight.startswith("N") else "unknown"


def to_number(value):
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize(ac: dict, lat: float, lon: float) -> dict | None:
    """Flatten one upstream record into the shape the phone renders."""
    a_lat, a_lon = to_number(ac.get("lat")), to_number(ac.get("lon"))
    if a_lat is None or a_lon is None:
        return None

    on_ground = ac.get("alt_baro") == "ground"
    alt = None if on_ground else to_number(ac.get("alt_baro"))
    if alt is None and not on_ground:
        alt = to_number(ac.get("alt_geom"))

    vs = to_number(ac.get("baro_rate"))
    if vs is None:
        vs = to_number(ac.get("geom_rate"))

    dist = to_number(ac.get("dst"))
    if dist is None:
        dist = haversine_nm(lat, lon, a_lat, a_lon)
    brg = to_number(ac.get("dir"))
    if brg is None:
        brg = bearing_deg(lat, lon, a_lat, a_lon)

    squawk = (ac.get("squawk") or "").strip()
    emergency = ac.get("emergency")
    if emergency in (None, "none", ""):
        emergency = EMERGENCY_SQUAWKS.get(squawk)

    return {
        "hex": (ac.get("hex") or "").strip().lower(),
        "flight": (ac.get("flight") or "").strip().upper(),
        "reg": (ac.get("r") or "").strip().upper(),
        "type": (ac.get("t") or "").strip().upper(),
        "desc": (ac.get("desc") or "").strip(),
        "owner": (ac.get("ownOp") or "").strip(),
        "cat": (ac.get("category") or "").strip().upper(),
        "lat": a_lat,
        "lon": a_lon,
        "alt": alt,
        "gnd": on_ground,
        "gs": to_number(ac.get("gs")),
        "trk": to_number(ac.get("track")) if ac.get("track") is not None else to_number(ac.get("true_heading")),
        "vs": vs,
        "squawk": squawk,
        "emergency": emergency,
        "mil": bool(int(ac.get("dbFlags") or 0) & 1),
        "dst": round(dist, 2),
        "dir": round(brg, 1),
        "seen": to_number(ac.get("seen_pos")) or to_number(ac.get("seen")) or 0,
        "class": classify(ac),
    }


def get_aircraft(lat: float, lon: float, radius: float) -> dict:
    # Round the cache key so small GPS jitter still reuses one upstream call.
    key = (round(lat, 2), round(lon, 2), round(radius))
    cached = aircraft_cache.get(key)
    if cached:
        return {**cached, "cached": True}

    errors = []
    for provider in PROVIDERS:
        url = provider["url"].format(lat=f"{lat:.4f}", lon=f"{lon:.4f}", radius=int(radius))
        started = time.time()
        try:
            raw = fetch_json(url)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            errors.append(f"{provider['name']}: {detail}")
            note_provider(provider["name"], False, detail)
            continue

        rows = raw.get("ac") or raw.get("aircraft") or []
        aircraft = [n for n in (normalize(r, lat, lon) for r in rows) if n]
        aircraft.sort(key=lambda a: a["dst"])
        note_provider(provider["name"], True, f"{len(aircraft)} aircraft in {time.time() - started:.2f}s")

        result = {
            "aircraft": aircraft,
            "source": provider["name"],
            "now": raw.get("now") or time.time() * 1000,
            "center": {"lat": lat, "lon": lon, "radius": radius},
            "errors": errors,
        }
        aircraft_cache.set(key, result)
        return {**result, "cached": False}

    raise RuntimeError("; ".join(errors) or "no providers configured")


def _route_entry(codes: str, airline, number, airports: list) -> dict:
    return {
        "route": codes,
        "airline": airline,
        "number": number,
        "airports": airports,
    }


def _routes_via_adsblol(batch: list) -> dict:
    """Batch lookup. Returns only the callsigns the upstream answered for."""
    rows = fetch_json(ROUTE_URL, payload={"planes": batch}, timeout=10.0)
    if not isinstance(rows, list):
        raise ValueError(f"expected a list, got {type(rows).__name__}")

    found = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        callsign = (row.get("callsign") or "").strip().upper()
        if not callsign:
            continue
        codes = row.get("_airport_codes_iata") or ""
        # `plausible` 0 means the upstream matched a number but does not trust
        # it. Showing a wrong route is worse than showing none.
        if not row.get("plausible") or codes in ("unknown", ""):
            found[callsign] = None
            continue
        airports = [
            {
                "iata": a.get("iata"),
                "icao": a.get("icao"),
                "name": a.get("name"),
                "location": a.get("location"),
                "countryiso2": a.get("countryiso2"),
            }
            for a in (row.get("_airports") or [])
        ]
        found[callsign] = _route_entry(codes, row.get("airline_code"), row.get("number"), airports)
    return found


def _adsbdb_airport(node: dict) -> dict:
    return {
        "iata": node.get("iata_code"),
        "icao": node.get("icao_code"),
        "name": node.get("name"),
        "location": node.get("municipality"),
        "countryiso2": node.get("country_iso_name"),
    }


def _routes_via_adsbdb(batch: list) -> dict:
    """One callsign per request. Capped, because a crowded sky would otherwise
    mean dozens of sequential round trips."""
    found = {}
    for plane in batch[:ADSBDB_MAX_LOOKUPS]:
        callsign = plane["callsign"]
        try:
            data = fetch_json(ADSBDB_URL.format(callsign=urllib.parse.quote(callsign)), timeout=8.0)
        except urllib.error.HTTPError as exc:
            # 404 is a definitive "no such callsign", not a transport failure.
            if exc.code == 404:
                found[callsign] = None
                continue
            raise

        response = data.get("response") if isinstance(data, dict) else None
        # An unknown callsign comes back as the string "unknown callsign".
        flight = response.get("flightroute") if isinstance(response, dict) else None
        if not flight:
            found[callsign] = None
            continue

        origin = flight.get("origin") or {}
        destination = flight.get("destination") or {}
        if not origin or not destination:
            found[callsign] = None
            continue

        airports = [_adsbdb_airport(origin)]
        midpoint = flight.get("midpoint")
        if midpoint:
            airports.append(_adsbdb_airport(midpoint))
        airports.append(_adsbdb_airport(destination))

        codes = "-".join(a["iata"] or a["icao"] or "?" for a in airports)
        airline = (flight.get("airline") or {}).get("icao")
        found[callsign] = _route_entry(codes, airline, flight.get("callsign_iata"), airports)
    return found


ROUTE_PROVIDERS = [
    ("adsb.lol", _routes_via_adsblol),
    ("adsbdb.com", _routes_via_adsbdb),
]


def get_routes(planes: list) -> dict:
    """Resolve callsigns to ORD-LAX style routes. ADS-B never carries the route,
    so this is a separate lookup, and it is very cacheable.

    Returns both the resolved routes and any upstream errors: a caller that
    cannot tell "not looked up yet" from "the lookup failed" has no way to stop
    showing a spinner forever.
    """
    errors = []
    out, unknown = {}, []
    for plane in planes:
        callsign = (plane.get("callsign") or "").strip().upper()
        if not callsign:
            continue
        cached = route_cache.get(callsign)
        if cached is not None:
            out[callsign] = cached
        else:
            unknown.append({
                "callsign": callsign,
                "lat": to_number(plane.get("lat")) or 0,
                "lng": to_number(plane.get("lng")) or to_number(plane.get("lon")) or 0,
            })

    for batch_start in range(0, len(unknown), 100):
        batch = unknown[batch_start:batch_start + 100]
        found = None
        for name, lookup in ROUTE_PROVIDERS:
            try:
                found = lookup(batch)
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                detail = f"{name}: {type(exc).__name__}: {exc}"
                errors.append(detail)
                note_provider("routes", False, detail)
                continue
            note_provider("routes", True, f"{name} resolved {len(found)} of {len(batch)}")
            break

        if found is None:
            # Every provider failed. Do not negative-cache: that would hide the
            # callsign behind a "no route on file" for the next half hour.
            continue

        for callsign, entry in found.items():
            route_cache.set(callsign, entry, ttl=ROUTE_TTL if entry else 60 * 60)
            out[callsign] = entry

        # Negative-cache the ones the provider simply did not answer for, so we
        # stop asking about them on every refresh.
        for plane in batch:
            if plane["callsign"] not in found:
                route_cache.set(plane["callsign"], None, ttl=60 * 30)
                out[plane["callsign"]] = None

    return {"routes": out, "errors": errors}


# --------------------------------------------------------------- http serving

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "FlightWall"
    protocol_version = "HTTP/1.1"
    token = None

    def log_message(self, fmt, *args):  # quieter than the default
        if self.path.startswith("/api/") and "error" in fmt.lower():
            log(fmt % args)

    # -- helpers

    def send_json(self, payload, status=HTTPStatus.OK, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, query) -> bool:
        if not self.token:
            return True
        supplied = (query.get("k") or [None])[0]
        if supplied and secrets.compare_digest(supplied, self.token):
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "fw_token" and secrets.compare_digest(value, self.token):
                return True
        return False

    def serve_file(self, rel: str, set_cookie: str | None = None):
        # Resolve first, then confirm the result is still inside web/ - this is
        # what stops `../../` from walking out of the served directory.
        target = (WEB / rel.lstrip("/")).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # The app polls constantly; stale HTML/JS after an update is worse than
        # a few extra bytes on the wire.
        self.send_header("Cache-Control", "no-cache" if target.suffix in (".html", ".js", ".css", ".webmanifest") else "max-age=86400")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    # -- routes

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path == "/api/health":
            self.send_json({
                "ok": True,
                "version": "1.0",
                "providers": provider_health,
                "auth": bool(self.token),
                "time": time.time(),
            })
            return

        if not self.authorized(query):
            if path in ("/", "/index.html"):
                self.send_error(HTTPStatus.UNAUTHORIZED, "Add ?k=<token> to the URL")
            else:
                self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        if path == "/api/landmarks":
            try:
                lat = float((query.get("lat") or [""])[0])
                lon = float((query.get("lon") or [""])[0])
            except ValueError:
                self.send_json({"error": "lat and lon are required"}, HTTPStatus.BAD_REQUEST)
                return
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                self.send_json({"error": "lat/lon out of range"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                radius = float((query.get("radius") or ["60"])[0])
            except ValueError:
                radius = 60.0
            radius = max(1.0, min(MAX_RADIUS_NM, radius))
            # Coarser key than aircraft: ground features do not need refreshing
            # for every small change in position.
            key = (round(lat, 2), round(lon, 2), round(radius))
            cached = landmark_cache.get(key)
            if cached is None:
                cached = landmarks.query(lat, lon, radius)
                landmark_cache.set(key, cached)
            self.send_json(cached)
            return

        if path == "/api/aircraft":
            try:
                lat = float((query.get("lat") or [""])[0])
                lon = float((query.get("lon") or [""])[0])
            except ValueError:
                self.send_json({"error": "lat and lon are required"}, HTTPStatus.BAD_REQUEST)
                return
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                self.send_json({"error": "lat/lon out of range"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                radius = float((query.get("radius") or ["60"])[0])
            except ValueError:
                radius = 60.0
            radius = max(1.0, min(MAX_RADIUS_NM, radius))
            try:
                self.send_json(get_aircraft(lat, lon, radius))
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return

        # Once the token is in the URL, hand back a cookie so the home-screen
        # icon keeps working without the query string hanging around.
        cookie = None
        if self.token and (query.get("k") or [None])[0] == self.token:
            cookie = f"fw_token={self.token}; Path=/; Max-Age=31536000; SameSite=Lax"

        if path == "/":
            self.serve_file("index.html", set_cookie=cookie)
            return
        self.serve_file(path, set_cookie=cookie)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self.authorized(urllib.parse.parse_qs(parsed.query)):
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        if parsed.path != "/api/routes":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("payload too large")
            payload = json.loads(self.rfile.read(length).decode() or "{}")
            planes = payload.get("planes") or []
            if not isinstance(planes, list):
                raise ValueError("planes must be a list")
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(get_routes(planes[:200]))


# ----------------------------------------------------------------- entrypoint

def load_token(disable_auth: bool) -> str | None:
    """One long-lived token, generated once and kept out of the repo. Matters
    the moment this is exposed through a public tunnel."""
    if disable_auth:
        return None
    env_token = os.environ.get("FLIGHTWALL_TOKEN")
    if env_token:
        return env_token
    STATE.mkdir(parents=True, exist_ok=True)
    config_path = STATE / "config.json"
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError):
        config = {}
    if not config.get("token"):
        config["token"] = secrets.token_urlsafe(16)
        config_path.write_text(json.dumps(config, indent=2))
        os.chmod(config_path, 0o600)
    return config["token"]


def ensure_self_signed(cert: Path, key: Path) -> None:
    if cert.exists() and key.exists():
        return
    cert.parent.mkdir(parents=True, exist_ok=True)
    log("generating a self-signed certificate (10 years)")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "3650",
            "-subj", "/CN=flightwall.local",
            "-addext", "subjectAltName=DNS:flightwall.local,DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(key, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the FlightWall board.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8730)
    parser.add_argument("--tls", action="store_true", help="serve HTTPS with a self-signed cert")
    parser.add_argument("--cert", default=str(STATE / "cert.pem"))
    parser.add_argument("--key", default=str(STATE / "key.pem"))
    parser.add_argument("--no-auth", action="store_true", help="drop the token check (LAN only)")
    parser.add_argument("--demo", action="store_true", help="synthetic traffic, no network needed")
    args = parser.parse_args()

    if args.demo:
        import demo_feed

        demo_feed.install(sys.modules[__name__])
        log("demo mode: serving synthetic aircraft, not real traffic")
    else:
        route_cache.load(STATE / "routes.json")

    Handler.token = load_token(args.no_auth)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    scheme = "http"
    if args.tls:
        cert, key = Path(args.cert), Path(args.key)
        ensure_self_signed(cert, key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"

    suffix = f"/?k={Handler.token}" if Handler.token else "/"
    log(f"FlightWall listening on {scheme}://{args.host}:{args.port}{suffix}")
    if Handler.token:
        log(f"token: {Handler.token}  (also in {STATE / 'config.json'})")
    else:
        log("auth disabled - do not expose this to the internet")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        # Never let synthetic routes leak into the real on-disk cache.
        if not args.demo:
            route_cache.dump(STATE / "routes.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
