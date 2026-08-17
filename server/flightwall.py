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
import re
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

import airports
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

# FlightAware, the only source here that knows what today's date is. Everything
# else in this file answers from a table built at some point in the past.
AEROAPI_URL = "https://aeroapi.flightaware.com/aeroapi/flights/{ident}"
AEROAPI_TTL = 60 * 60 * 6

# It is also the only source that costs money, and this board is the wrong
# shape for that. GET /flights/{ident} is $0.005 per result set against a $5
# monthly allowance, while the board changes aircraft every nine seconds and
# 296 new callsigns an hour cross a 60nm circle (measured over 5 minutes, 11
# samples). Querying whatever happened to be on screen tied spending to the
# sky rather than to anyone's interest in it, and ran to hundreds of dollars a
# month. A query is now spent only when the client names an aircraft in
# `live`, which it does once on open and once per tap. The cap below is the
# backstop for that policy failing, not the policy itself.
AEROAPI_QUERY_COST = 0.005
AEROAPI_MONTHLY_CAP = 800  # ~$4.00, comfortably inside the free allowance
AEROAPI_USAGE_FILE = "aeroapi_usage.json"

# The community route tables. All three are a flight number mapped to the
# airports that number meant when the table was built, with no date attached,
# so a reassigned number keeps its old airports indefinitely. Kept because
# they cost nothing and are occasionally right, never trusted on their own.
#
# adsb.lol's own batch endpoint (api/0/routeset) now answers HTTP 201 with an
# empty body for every callsign, and its route path redirects here marked
# deprecated, so the underlying file is fetched directly instead.
VRS_URL = "https://vrs-standing-data.adsb.lol/routes/{prefix}/{callsign}.json"
ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
ADSBDB_MAX_LOOKUPS = 20
VRS_MAX_LOOKUPS = 40

AIRCRAFT_TTL = 4.0  # seconds; upstreams update about this often
ROUTE_TTL = 60 * 60 * 24  # one day: any longer just preserves stale records
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

# Identifies this server process. Every deploy is a restart, so a change in
# this value tells a long-running client its own code may be out of date.
BOOT_ID = f"{int(time.time())}-{os.getpid()}"


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
# Whichever feed last actually produced aircraft. A provider can stay up and
# fast while returning nothing useful for days, and asking it first every time
# just adds a wasted round trip to every refresh.
_preferred_provider: str | None = None


def provider_order() -> list:
    """Providers, best-known-good first, otherwise in configured order.

    Self-correcting in both directions: a feed that goes quiet stops being
    asked first, and the moment it produces aircraft again it is preferred
    again. Nothing to edit when an upstream recovers.
    """
    with _provider_lock:
        preferred = _preferred_provider
    if not preferred:
        return list(PROVIDERS)
    # sorted() is stable, so everything else keeps its configured order.
    return sorted(PROVIDERS, key=lambda p: 0 if p["name"] == preferred else 1)


def note_preferred(name: str) -> None:
    global _preferred_provider
    with _provider_lock:
        _preferred_provider = name


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
        # Autopilot selected altitude. Unlike vertical speed it does not
        # flicker, so it is what tells an aircraft levelled off mid-descent
        # apart from one that is genuinely cruising.
        "sel": to_number(ac.get("nav_altitude_mcp")),
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
    empty_result = None  # a provider that answered cleanly but saw nothing
    for provider in provider_order():
        url = provider["url"].format(lat=f"{lat:.4f}", lon=f"{lon:.4f}", radius=int(radius))
        started = time.time()
        try:
            raw = fetch_json(url)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            errors.append(f"{provider['name']}: {detail}")
            note_provider(provider["name"], False, detail)
            continue

        # "Key present but empty" means a genuinely quiet sky. "Key absent"
        # means this is not the response we think it is - a rate-limit notice,
        # an error object, a changed API - and reporting it as zero aircraft
        # would quietly claim there is nothing overhead when there is.
        if isinstance(raw, dict) and isinstance(raw.get("ac"), list):
            rows = raw["ac"]
        elif isinstance(raw, dict) and isinstance(raw.get("aircraft"), list):
            rows = raw["aircraft"]
        else:
            shape = ", ".join(sorted(raw)[:6]) if isinstance(raw, dict) else type(raw).__name__
            detail = f"no aircraft list in response (got: {shape})"
            errors.append(f"{provider['name']}: {detail}")
            note_provider(provider["name"], False, detail)
            continue

        aircraft = [n for n in (normalize(r, lat, lon) for r in rows) if n]
        aircraft.sort(key=lambda a: a["dst"])
        note_provider(provider["name"], True, f"{len(aircraft)} aircraft in {time.time() - started:.2f}s")

        result = {
            "aircraft": aircraft,
            "source": provider["name"],
            "now": raw.get("now") or time.time() * 1000,
            "center": {"lat": lat, "lon": lon, "radius": radius},
            "errors": errors,
            # Which server process answered. The phone compares this across
            # polls and reloads itself when it changes, because a home-screen
            # app resumes rather than reloading: after an update landed, a
            # phone kept running week-old JavaScript against the new server,
            # and taps quietly did nothing the new code was supposed to do.
            "boot": BOOT_ID,
        }

        # An empty sky is possible but unusual, and it looks identical to a
        # provider having a bad day. Ask the next one before believing it;
        # only if they all agree is the sky really empty.
        if not aircraft:
            empty_result = empty_result or result
            errors.append(f"{provider['name']}: answered with zero aircraft")
            continue

        note_preferred(provider["name"])
        aircraft_cache.set(key, result)
        return {**result, "cached": False}

    if empty_result is not None:
        empty_result = {**empty_result, "errors": errors, "allProvidersEmpty": True}
        aircraft_cache.set(key, empty_result)
        return {**empty_result, "cached": False}

    raise RuntimeError("; ".join(errors) or "no providers configured")


def _route_entry(codes: str, airline, number, airports: list) -> dict:
    return {
        "route": codes,
        "airline": airline,
        "number": number,
        "airports": airports,
    }


def _routes_via_vrs(batch: list) -> dict:
    """VRS standing data, one file per callsign, straight off the CDN.

    This is the table adsb.lol serves; its own batch endpoint returns an empty
    body now, so the files are read directly. Static, undated, and wrong often
    enough that every answer still has to clear the position check.
    """
    found = {}
    for plane in batch[:VRS_MAX_LOOKUPS]:
        callsign = plane["callsign"]
        prefix = callsign[:2].upper()
        if not prefix.isalnum():
            continue
        try:
            row = fetch_json(
                VRS_URL.format(prefix=urllib.parse.quote(prefix),
                               callsign=urllib.parse.quote(callsign)),
                timeout=8.0,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                found[callsign] = None  # definitively not in the table
                continue
            raise

        if not isinstance(row, dict):
            continue
        codes = row.get("_airport_codes_iata") or ""
        if codes in ("unknown", ""):
            found[callsign] = None
            continue
        stops = [
            {
                "iata": a.get("iata"),
                "icao": a.get("icao"),
                "name": a.get("name"),
                "location": a.get("location"),
                "countryiso2": a.get("countryiso2"),
                "lat": to_number(a.get("lat") if a.get("lat") is not None else a.get("latitude")),
                "lon": to_number(a.get("lon") if a.get("lon") is not None else a.get("longitude")),
            }
            for a in (row.get("_airports") or [])
        ]
        if len(stops) < 2:
            found[callsign] = None
            continue
        found[callsign] = _route_entry(codes, row.get("airline_code"), row.get("number"), stops)
    return found


# ------------------------------------------------------------------- aeroapi

def aeroapi_key() -> str | None:
    """The FlightAware key, if the owner has set one up.

    Absent is a normal state, not an error: without it the board falls back to
    what the aircraft itself broadcasts, which costs nothing and needs no
    account.
    """
    key = os.environ.get("FLIGHTWALL_AEROAPI_KEY")
    if key:
        return key.strip()
    try:
        config = json.loads((STATE / "config.json").read_text())
    except (OSError, ValueError):
        return None
    key = config.get("aeroapi_key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _aeroapi_airport(node: dict) -> dict | None:
    if not isinstance(node, dict):
        return None
    icao = node.get("code_icao") or None
    iata = node.get("code_iata") or None
    code = node.get("code") or None
    if not (icao or iata or code):
        return None
    # FlightAware does not return coordinates on this endpoint, so they come
    # from the local airport table. Without them the position check has
    # nothing to measure, which is fine: this source does not need checking.
    fix = None
    for candidate in (icao, code, iata):
        if candidate:
            fix = _airport_by_code(candidate)
            if fix:
                break
    return {
        "iata": iata or (fix or {}).get("iata"),
        "icao": icao or (fix or {}).get("icao"),
        "name": node.get("name") or (fix or {}).get("name"),
        "location": node.get("city") or (fix or {}).get("location"),
        "lat": (fix or {}).get("lat"),
        "lon": (fix or {}).get("lon"),
    }


_code_index: dict | None = None


def _airport_by_code(code: str) -> dict | None:
    """Look a code up in the bundled airport table, by ICAO then IATA."""
    global _code_index
    if _code_index is None:
        index = {}
        for row in airports._load():  # noqa: SLF001 - same project, one dataset
            if row[airports.I_ICAO]:
                index.setdefault(row[airports.I_ICAO], row)
            if row[airports.I_IATA]:
                index.setdefault(row[airports.I_IATA], row)
        _code_index = index
    row = _code_index.get((code or "").strip().upper())
    return airports.as_dict(row) if row else None


def _pick_current_flight(flights: list) -> dict | None:
    """Which of the ~14 days of flights under this number is the one overhead.

    Airborne beats everything: actual wheels-up with no wheels-down yet. After
    that, the one that has most recently departed. A flight number that has
    not flown today is not an answer at all - that is precisely the failure
    the static tables make.
    """
    airborne = [
        f for f in flights
        if isinstance(f, dict) and f.get("actual_off") and not f.get("actual_on")
    ]
    if airborne:
        return max(airborne, key=lambda f: f.get("actual_off") or "")
    recent = [f for f in flights if isinstance(f, dict) and f.get("actual_off")]
    if not recent:
        return None
    newest = max(recent, key=lambda f: f.get("actual_off") or "")
    # Landed already; only worth reporting while it is still the same day's
    # movement, otherwise it is just another stale record with a nicer source.
    return newest if not newest.get("actual_on") else None


def _route_via_aeroapi(callsign: str) -> dict | None:
    """Today's actual route for one callsign, from FlightAware.

    The one lookup in this file that is date-aware, and the only one whose
    answer does not need checking against the aircraft's position.
    """
    key = aeroapi_key()
    if not key:
        return None

    # An airline callsign is an operator code followed by a flight number
    # (UAL2402); anything else on the board is a registration (N330JT).
    # Telling FlightAware which it is got wrong answers for private aircraft,
    # because a tail number is not a designator and it will not resolve one.
    designator = bool(re.fullmatch(r"[A-Z]{3}\d{1,4}[A-Z]?", callsign))
    ident_type = "designator" if designator else "registration"
    url = f"{AEROAPI_URL.format(ident=urllib.parse.quote(callsign))}?ident_type={ident_type}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json; charset=UTF-8",
        "x-apikey": key,
    })
    with urllib.request.urlopen(req, timeout=12.0) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))

    flight = _pick_current_flight(data.get("flights") or [])
    if not flight:
        return None

    origin = _aeroapi_airport(flight.get("origin"))
    destination = _aeroapi_airport(flight.get("destination"))
    if not origin or not destination:
        return None

    stops = [origin, destination]
    codes = "-".join(a["iata"] or a["icao"] or "?" for a in stops)
    entry = _route_entry(codes, flight.get("operator_icao") or flight.get("operator"),
                         flight.get("flight_number"), stops)
    entry["source"] = "aeroapi"
    entry["confidence"] = "confirmed"
    entry["progress"] = flight.get("progress_percent")
    entry["status"] = flight.get("status")
    return entry


def _adsbdb_airport(node: dict) -> dict:
    return {
        "iata": node.get("iata_code"),
        "icao": node.get("icao_code"),
        "name": node.get("name"),
        "location": node.get("municipality"),
        "countryiso2": node.get("country_iso_name"),
        "lat": to_number(node.get("latitude")),
        "lon": to_number(node.get("longitude")),
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
    ("adsbdb.com", _routes_via_adsbdb),
    ("vrs-standing-data", _routes_via_vrs),
]

# How far off the claimed path an aircraft may be before the route is treated
# as a bad record. Generous, because real flights hold, divert and get vectored
# around weather - this is meant to catch a route that is simply wrong, not to
# police routine deviation.
ROUTE_SLACK_NM = 60.0
ROUTE_SLACK_FRACTION = 0.15


def _leg_excess_nm(lat, lon, a: dict, b: dict):
    """How far off a direct A-to-B leg the aircraft is, in nautical miles.

    Uses the ellipse property: for a point on the line between two foci, the
    summed distance equals the separation. Anything further away is off the
    path, and the excess grows smoothly with the deviation.
    """
    if None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return None
    leg = haversine_nm(a["lat"], a["lon"], b["lat"], b["lon"])
    detour = haversine_nm(lat, lon, a["lat"], a["lon"]) + haversine_nm(lat, lon, b["lat"], b["lon"])
    return detour - leg, leg


def route_is_plausible(entry: dict, lat, lon) -> bool:
    """Does this aircraft's actual position support the claimed route?

    Route databases are keyed on flight number and go stale: a reused or
    retired number keeps its old airports, and the result is a confident,
    wrong answer. The aircraft is broadcasting where it really is, so use
    that as the arbiter.

    Multi-leg routes are checked leg by leg, since an aircraft partway
    through A-B-C can sit far off the direct A-to-C line quite legitimately.
    """
    airports = (entry or {}).get("airports") or []
    if len(airports) < 2 or lat is None or lon is None:
        return True  # nothing to check against; do not invent a verdict

    best = None
    for first, second in zip(airports, airports[1:]):
        measured = _leg_excess_nm(lat, lon, first, second)
        if measured is None:
            return True  # missing coordinates, so no basis to reject
        excess, leg = measured
        allowed = max(ROUTE_SLACK_NM, ROUTE_SLACK_FRACTION * leg)
        if excess <= allowed:
            return True
        best = excess if best is None else min(best, excess)
    return False


ROUTE_CANDIDATES = {
    "adsbdb.com": lambda cs: (ADSBDB_URL.format(callsign=urllib.parse.quote(cs)), None),
    "vrs-standing": lambda cs: (
        VRS_URL.format(prefix=urllib.parse.quote(cs[:2].upper()),
                       callsign=urllib.parse.quote(cs)), None),
    "hexdb.io": lambda cs: (f"https://hexdb.io/api/v1/route/icao/{urllib.parse.quote(cs)}", None),
}


def check_routes(callsigns: list) -> int:
    """Ask every known route source about the same callsigns and print what
    each one says, verbatim.

    Route data is the one part of this app with no way to tell right from
    wrong on its own - a stale record looks exactly like a good one. This
    exists so a source can be judged on real output rather than on its
    documentation.
    """
    for callsign in callsigns:
        callsign = callsign.strip().upper()
        print(f"\n{callsign}")
        print("-" * (len(callsign) + 2))
        for name, build in ROUTE_CANDIDATES.items():
            url, payload = build(callsign)
            try:
                raw = fetch_json(url, payload=payload, timeout=12.0)
            except Exception as exc:  # noqa: BLE001 - this is a diagnostic
                print(f"  {name:<12} FAILED  {type(exc).__name__}: {exc}")
                continue
            body = json.dumps(raw, separators=(",", ":"))
            print(f"  {name:<12} {body[:400]}{'…' if len(body) > 400 else ''}")
    print()
    return 0


_usage_lock = threading.Lock()


def aeroapi_usage() -> dict:
    """Queries spent this calendar month, kept on disk.

    A counter held only in memory would reset every time the agent restarted,
    which on an always-on laptop is exactly when nobody is watching. The month
    is part of the record so it rolls over on its own.
    """
    month = time.strftime("%Y-%m")
    path = STATE / AEROAPI_USAGE_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if data.get("month") != month:
        data = {"month": month, "queries": 0}
    return data


def _spend_aeroapi_query() -> bool:
    """Claim one query against this month's cap. False means do not call."""
    with _usage_lock:
        data = aeroapi_usage()
        if data["queries"] >= AEROAPI_MONTHLY_CAP:
            return False
        data["queries"] += 1
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            (STATE / AEROAPI_USAGE_FILE).write_text(json.dumps(data))
        except OSError:
            # Losing the count is not a reason to stop serving the board, but
            # it does mean the cap stops protecting anything, so say so.
            log("WARNING: could not record AeroAPI usage; the cap is not enforced")
        return True


def _codes_of(airport: dict) -> set:
    return {c for c in (airport.get("icao"), airport.get("iata")) if c}


def reconcile_with_trace(entry: dict, hex_id: str) -> dict | None:
    """Judge a table route against where the aircraft actually took off.

    The position check cannot tell a route from its own reverse: A-to-B and
    B-to-A are the same line on the ground, so an aircraft flying the return
    leg passes the check with its origin and destination swapped, and the
    board shows the flight backwards with full confidence. Measured on live
    traffic, that and outright wrong records account for the majority of the
    routes that survive the geometric check.

    The aircraft's own track history settles it, because a departure airport
    is an observation:

      matches the claimed origin       the origin is right; the destination
                                       is still only the table's word
      matches the claimed destination  the record is some other leg of this
                                       number's day, so drop it
      matches neither                  the record is about a different flight

    An earlier version treated the second case as a return leg and reversed
    the record. That manufactured destinations: SWA569's table entry was
    BOS-BNA, the aircraft departed BNA, and the flip produced BNA-BOS while
    the flight was actually BNA-RIC - numbers like Southwest's fly several
    legs a day, and departing the recorded destination proves the record is
    the wrong leg, not the same leg backwards. The trace proves where an
    aircraft took off. It proves nothing about where it is going, and half an
    observation must not dress up a guess.

    Returns the entry, or None if the record cannot be trusted.
    """
    stops = (entry or {}).get("airports") or []
    if len(stops) < 2:
        return entry

    observed = airports.origin_from_trace(hex_id)
    if not observed:
        return entry  # nothing observed, so no opinion either way

    seen = _codes_of(observed)
    if not seen:
        return entry

    if seen & _codes_of(stops[0]):
        return {**entry, "confidence": "trace-confirmed"}

    return None


def get_routes(planes: list, focus: str | None = None, live: str | None = None) -> dict:
    """Resolve callsigns to ORD-LAX style routes. ADS-B never carries the route,
    so this is a separate lookup, and it is very cacheable.

    Three sources, in descending order of how much they can be trusted:

      FlightAware   today's actual flight. Costs a query, so it is asked only
                    about `live`, and `live` is only ever set because somebody
                    asked for that aircraft. Cycling past one costs nothing.
      route tables  free, undated, and wrong most of the time; every answer
                    has to survive the position check, and the aircraft on the
                    board is judged against its own departure on top of that.
      the aircraft  its own descent and its own track history. Never stale,
                    because it is an observation rather than a record.

    Returns the resolved routes, the derived origin and destination keyed by
    hex, and any upstream errors: a caller that cannot tell "not looked up
    yet" from "the lookup failed" has no way to stop showing a spinner.
    """
    errors = []
    out = {}
    positions = {}
    unknown = []
    focus = (focus or "").strip().upper() or None
    live = (live or "").strip().upper() or None

    for plane in planes:
        callsign = (plane.get("callsign") or "").strip().upper()
        if not callsign:
            continue
        lat = to_number(plane.get("lat"))
        lon = to_number(plane.get("lng"))
        if lon is None:
            lon = to_number(plane.get("lon"))
        positions[callsign] = (lat, lon)

        cached = route_cache.get(callsign)
        if cached is not None:
            out[callsign] = cached
        else:
            unknown.append({"callsign": callsign, "lat": lat or 0, "lng": lon or 0})

    for batch_start in range(0, len(unknown), 100):
        batch = unknown[batch_start:batch_start + 100]
        pending = {p["callsign"]: p for p in batch}
        rejected = {}  # answers contradicted by the aircraft's own position
        answered = False

        for name, lookup in ROUTE_PROVIDERS:
            if not pending:
                break
            try:
                found = lookup(list(pending.values()))
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                detail = f"{name}: {type(exc).__name__}: {exc}"
                errors.append(detail)
                note_provider("routes", False, detail)
                continue

            answered = True
            accepted = 0
            for callsign, entry in found.items():
                plane = pending.get(callsign)
                if plane is None:
                    continue
                if not entry:
                    # This source does not know it. Another one might, so leave
                    # the callsign pending rather than settling for nothing.
                    continue
                if not route_is_plausible(entry, plane["lat"], plane["lng"]):
                    # A wrong answer is not an answer. Hold on to it in case
                    # every source is wrong, but keep asking.
                    rejected[callsign] = entry
                    continue
                out[callsign] = entry
                route_cache.set(callsign, entry, ttl=ROUTE_TTL)
                pending.pop(callsign, None)
                accepted += 1
            note_provider("routes", True, f"{name} resolved {accepted} of {len(batch)}")

        if not answered:
            # Every provider errored. Do not negative-cache: that would hide
            # the callsign behind "no route on file" for the next half hour.
            continue

        for callsign in pending:
            if callsign in rejected:
                # Every source that had an answer was contradicted by the
                # aircraft's position. Report the best of a bad set, flagged.
                entry = {**rejected[callsign], "suspect": True}
                out[callsign] = entry
                route_cache.set(callsign, entry, ttl=60 * 60)
            else:
                out[callsign] = None
                route_cache.set(callsign, None, ttl=60 * 30)

    # Cached entries were checked against wherever the aircraft was when the
    # record was first fetched, which is not where it is now.
    for callsign, entry in list(out.items()):
        if not entry or entry.get("suspect"):
            continue
        if entry.get("source") == "aeroapi":
            # Checked against nothing: this one names today's flight, and an
            # aircraft holding or being vectored is still on that flight.
            continue
        lat, lon = positions.get(callsign, (None, None))
        if not route_is_plausible(entry, lat, lon):
            out[callsign] = {**entry, "suspect": True}

    # The aircraft on the board gets the stronger test. The geometric check
    # above only proves a route is not absurd; the aircraft's own departure
    # proves whether it is this flight, and which way round it is flying.
    # Not applied to the whole list because each check costs a trace fetch.
    derived = derive(planes, focus)

    if focus:
        hex_id = next(
            ((p.get("hex") or "").strip().lower() for p in planes
             if (p.get("callsign") or "").strip().upper() == focus and p.get("hex")),
            None,
        )
        if hex_id and out.get(focus):
            settled = reconcile_with_trace(out[focus], hex_id)
            # A record the aircraft contradicts outright is worse than a blank
            # panel: the derived origin and destination will answer instead.
            out[focus] = settled if settled else None

        # A confirmed departure still says nothing about the arrival - a
        # reused number's record can hold the right origin and a different
        # leg's destination (VRS held BNA-MCO for a flight running BNA-RIC).
        # The descent is the one arrival fact actually observed, so when the
        # aircraft is measurably going somewhere else, the record loses.
        entry = out.get(focus)
        if entry and hex_id and entry.get("source") != "aeroapi":
            inferred = (derived.get(hex_id) or {}).get("destination")
            stops = entry.get("airports") or []
            if inferred and stops and not (_codes_of(inferred) & _codes_of(stops[-1])):
                out[focus] = None

    # A paid query happens only when the board is told to make one, never
    # because an aircraft happened to come round on the cycle. The client asks
    # for exactly one on open, for whatever is nearest, and one more each time
    # somebody taps an aircraft.
    if live:
        confirmed = _live_route(live, errors)
        if confirmed:
            out[live] = confirmed

    return {"routes": out, "derived": derived, "errors": errors}


def _live_route(callsign: str, errors: list) -> dict | None:
    """FlightAware's answer for one callsign, within the monthly cap.

    Misses are cached alongside hits: a number FlightAware has no live flight
    for will not sprout one in the next few minutes, and asking again costs
    the same as asking the first time.
    """
    cached = route_cache.get(f"aero:{callsign}")
    if cached is not None:
        return cached or None

    if not aeroapi_key():
        return None

    if not _spend_aeroapi_query():
        note_provider("routes:live", False,
                      f"monthly cap of {AEROAPI_MONTHLY_CAP} queries reached")
        return None

    try:
        confirmed = _route_via_aeroapi(callsign)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        detail = f"aeroapi: {type(exc).__name__}: {exc}"
        errors.append(detail)
        note_provider("routes:live", False, detail)
        return None

    route_cache.set(f"aero:{callsign}", confirmed or False, ttl=AEROAPI_TTL)
    spent = aeroapi_usage()["queries"]
    note_provider("routes:live", True,
                  f"{'resolved' if confirmed else 'no live flight for'} {callsign}"
                  f" ({spent}/{AEROAPI_MONTHLY_CAP} this month)")
    return confirmed


def derive(planes: list, focus: str | None = None) -> dict:
    """What each aircraft says about itself, independent of any route table.

    Destination comes from the descent and costs nothing, so every aircraft
    gets one. Origin needs the aircraft's track history pulled over the wire,
    which is a few hundred KB, so only the focused aircraft is worth it.
    """
    focus = (focus or "").strip().upper() or None
    out = {}
    for plane in planes:
        hex_id = (plane.get("hex") or "").strip().lower()
        if not hex_id:
            continue
        # The board speaks `lng`; the readsb dialect everything else in this
        # file uses says `lon`. Normalise once here rather than teaching the
        # inference two spellings.
        plane = dict(plane)
        if plane.get("lon") is None:
            plane["lon"] = plane.get("lng")
        profile = airports.descent_profile(plane)
        entry = {
            "phase": profile["phase"],
            "altitude": profile["altitude"],
            "rate": profile["rate"],
            "destination": airports.infer_destination(plane),
            "origin": None,
        }
        if focus and (plane.get("callsign") or "").strip().upper() == focus:
            entry["origin"] = airports.origin_from_trace(hex_id)
        out[hex_id] = entry
    return out


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
            spent = aeroapi_usage()
            self.send_json({
                "ok": True,
                "version": "1.0",
                "providers": provider_health,
                # Whether the live-flight tier is even switched on. Without
                # this, a key that never loaded looks exactly like a key that
                # loaded and found no flights, and the board looks the same
                # either way: quietly falling back to the tables.
                "aeroapi": {
                    "configured": bool(aeroapi_key()),
                    "source": (
                        "env" if os.environ.get("FLIGHTWALL_AEROAPI_KEY")
                        else "config.json" if aeroapi_key()
                        else None
                    ),
                    # What has actually been spent. Without this the owner has
                    # to go and read FlightAware's own dashboard to answer
                    # "is this thing costing me anything", which is the one
                    # question the board should be able to answer itself.
                    "spent": spent["queries"],
                    "cap": AEROAPI_MONTHLY_CAP,
                    "month": spent["month"],
                    "estimatedCost": round(spent["queries"] * AEROAPI_QUERY_COST, 3),
                    # Names the spending rule, so which build is running can be
                    # read from outside rather than inferred.
                    "policy": "on-request",
                },
                "airports": airports.loaded(),
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
            focus = payload.get("focus")
            if focus is not None and not isinstance(focus, str):
                raise ValueError("focus must be a string")
            live = payload.get("live")
            if live is not None and not isinstance(live, str):
                raise ValueError("live must be a string")
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(get_routes(planes[:200], focus, live))


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
    parser.add_argument(
        "--check-routes", nargs="+", metavar="CALLSIGN",
        help="ask every route source about these callsigns and print the raw answers",
    )
    args = parser.parse_args()

    if args.check_routes:
        return check_routes(args.check_routes)

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
