"""A synthetic aircraft feed for `--demo`.

Lets you see the board without touching the network - useful for checking a
layout change, or for confirming the install works before worrying about
whether the upstream feeds are reachable.
"""

from __future__ import annotations

import math
import time

# (callsign, registration, type, description, category, bearing, nm, alt, gs, track, vs, dbFlags)
FLEET = [
    ("UAL2402", "N37502", "A21N", "AIRBUS A-321neo", "A3", 71, 9.4, 4100, 217, 263, -1088, 0),
    ("DAL1119", "N3768", "B739", "BOEING 737-900", "A3", 145, 21.0, 24000, 412, 95, 1900, 0),
    ("SWA88", "N8654B", "B38M", "BOEING 737 MAX 8", "A3", 210, 33.5, 31000, 448, 182, 0, 0),
    ("AAL717", "N993AN", "B788", "BOEING 787-8", "A5", 305, 48.2, 37000, 470, 271, 0, 0),
    ("FDX1284", "N104FE", "MD11", "BOEING MD-11F", "A5", 18, 57.1, 33000, 455, 12, -640, 0),
    ("N911MD", "N911MD", "EC35", "AIRBUS HELICOPTERS H135", "A7", 240, 4.1, 1200, 110, 15, 300, 0),
    ("N44RB", "N44RB", "SR22", "CIRRUS SR-22", "A1", 95, 12.7, 3000, 155, 340, -200, 0),
    ("RCH471", "02-1109", "C17", "BOEING C-17A", "A5", 330, 62.0, 22000, 380, 155, 0, 1),
    ("ACA795", "C-FGDT", "A333", "AIRBUS A-330-300", "A5", 60, 71.4, 35000, 462, 88, 0, 0),
    ("N682QS", "N682QS", "C68A", "CESSNA 680A Latitude", "A2", 155, 18.9, 15000, 330, 210, 2200, 0),
]

# callsign -> (airline, number, [(iata, icao, airport name, city, country), ...])
ROUTES = {
    "UAL2402": ("UAL", "2402", [
        ("ORD", "KORD", "Chicago O'Hare International", "Chicago", "US"),
        ("LAX", "KLAX", "Los Angeles International", "Los Angeles", "US"),
    ]),
    "DAL1119": ("DAL", "1119", [
        ("ATL", "KATL", "Hartsfield Jackson Atlanta International", "Atlanta", "US"),
        ("MSP", "KMSP", "Minneapolis St Paul International", "Minneapolis", "US"),
    ]),
    "SWA88": ("SWA", "88", [
        ("MDW", "KMDW", "Chicago Midway International", "Chicago", "US"),
        ("DEN", "KDEN", "Denver International", "Denver", "US"),
    ]),
    "AAL717": ("AAL", "717", [
        ("ORD", "KORD", "Chicago O'Hare International", "Chicago", "US"),
        ("LHR", "EGLL", "London Heathrow", "London", "GB"),
    ]),
    "FDX1284": ("FDX", "1284", [
        ("MEM", "KMEM", "Memphis International", "Memphis", "US"),
        ("IND", "KIND", "Indianapolis International", "Indianapolis", "US"),
        ("SEA", "KSEA", "Seattle Tacoma International", "Seattle", "US"),
    ]),
    "ACA795": ("ACA", "795", [
        ("YYZ", "CYYZ", "Toronto Pearson International", "Toronto", "CA"),
        ("ORD", "KORD", "Chicago O'Hare International", "Chicago", "US"),
    ]),
}


def _offset(lat: float, lon: float, bearing_deg: float, distance_nm: float):
    r = 3440.065
    b = math.radians(bearing_deg)
    d = distance_nm / r
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
    )
    return math.degrees(p2), math.degrees(l2)


def aircraft(lat: float, lon: float, radius: float) -> dict:
    """Lay the demo fleet out around the caller, drifting slowly so the board
    visibly updates."""
    drift = (time.time() / 60.0) % 360.0
    out = []
    for callsign, reg, typ, desc, cat, bearing, nm, alt, gs, track, vs, flags in FLEET:
        if nm > radius:
            continue
        a_lat, a_lon = _offset(lat, lon, (bearing + drift) % 360, nm)
        out.append({
            "hex": f"{abs(hash(callsign)) % 0xFFFFFF:06x}",
            "flight": f"{callsign} ",
            "r": reg,
            "t": typ,
            "desc": desc,
            "category": cat,
            "lat": a_lat,
            "lon": a_lon,
            "alt_baro": alt,
            "gs": gs,
            "track": (track + drift) % 360,
            "baro_rate": vs,
            "squawk": "1200",
            "dbFlags": flags,
            "dst": nm,
            "dir": (bearing + drift) % 360,
            "seen_pos": 0.3,
        })
    return {"ac": out, "now": time.time() * 1000, "total": len(out)}


def routes(planes: list) -> list:
    out = []
    for plane in planes:
        callsign = (plane.get("callsign") or "").strip().upper()
        entry = ROUTES.get(callsign)
        if not entry:
            continue
        airline, number, airports = entry
        out.append({
            "callsign": callsign,
            "airline_code": airline,
            "number": number,
            "_airport_codes_iata": "-".join(a[0] for a in airports),
            "plausible": 1,
            "_airports": [
                {"iata": iata, "icao": icao, "name": name, "location": city, "countryiso2": country}
                for iata, icao, name, city, country in airports
            ],
        })
    return out


def install(module) -> None:
    """Swap the module's single network entry point for the demo data."""

    def fake_fetch(url, payload=None, timeout=8.0):
        if payload is not None:
            return routes(payload.get("planes") or [])
        parts = [p for p in url.split("/") if p]
        # Both upstream URL shapes end with .../<lat>/<lon>/<radius>, give or
        # take the `lat`/`lon`/`dist` path words.
        numbers = []
        for part in parts:
            try:
                numbers.append(float(part))
            except ValueError:
                continue
        lat, lon, radius = (numbers + [41.97, -87.9, 60])[:3]
        return aircraft(lat, lon, radius)

    module.fetch_json = fake_fetch
