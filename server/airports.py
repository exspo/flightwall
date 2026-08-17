"""Where an aircraft came from and where it is going, worked out from the
aircraft itself.

Route databases are keyed on flight number and carry no date, so they answer
with whatever that number meant on the day the table was built. The aircraft,
meanwhile, is broadcasting its position, track, speed and vertical rate right
now, and it flew out of somewhere real a few hours ago. Both of those are
observations rather than records, and neither goes stale.

Two independent answers live here:

  origin       from the aircraft's own track history: rewind to the last time
               it sat on the ground and name the field it left.
  destination  from the descent: an aeroplane losing height at a known rate
               and speed is pointing at a specific patch of ground, and there
               is usually only one airport there.

Neither is authoritative. Both are honest about how sure they are, which is
the part a filed route cannot offer.
"""

from __future__ import annotations

import gzip
import json
import math
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "airports.json"

USER_AGENT = "FlightWall/1.0 (+https://github.com/exspo/flightwall)"

# adsb.lol keeps 24h of every aircraft it has seen. The file is served gzipped
# and runs to a few hundred KB, so it is only worth pulling for an aircraft
# the board is actually showing.
TRACE_URL = "https://adsb.lol/data/traces/{suffix}/trace_full_{hex}.json"

R_NM = 3440.065

# Fields: icao, iata, name, municipality, lat, lon, rank, scheduled
I_ICAO, I_IATA, I_NAME, I_CITY, I_LAT, I_LON, I_RANK, I_SCHED = range(8)

# An airliner descending towards a field is aiming at a runway, not at the
# centre of the airport property, and the descent estimate itself is coarse.
# These are the tolerances that stop a good guess being thrown away.
AIM_RADIUS_NM = 18.0        # how near the projected touchdown an airport must be
AHEAD_CONE_DEG = 55.0       # and roughly ahead, not behind the wing
MAX_PROJECTION_NM = 150.0   # beyond this the descent maths is guesswork
STEPDOWN_CEILING_FT = 25000.0  # above this, level flight is cruise, not arrival

_airports: list | None = None
_grid: dict = {}
_load_lock = threading.Lock()

_trace_cache: dict = {}
_trace_lock = threading.Lock()
TRACE_TTL = 60 * 10


# ------------------------------------------------------------------ geometry

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_between(a: float, b: float) -> float:
    """Smallest angle between two bearings, 0-180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def project(lat: float, lon: float, bearing: float, dist_nm: float) -> tuple:
    """Move along a great circle. Straight-line projection would drift badly
    over the few hundred miles a descent can cover."""
    d = dist_nm / R_NM
    b = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


# ------------------------------------------------------------------- dataset

def _load() -> list:
    global _airports
    if _airports is not None:
        return _airports
    with _load_lock:
        if _airports is not None:
            return _airports
        try:
            rows = json.loads(DATA.read_text())
        except (OSError, ValueError):
            # No dataset means no inference, not a crash. Run
            # scripts/build_airports.py to populate it.
            rows = []
        grid: dict = {}
        for row in rows:
            cell = (int(math.floor(row[I_LAT])), int(math.floor(row[I_LON])))
            grid.setdefault(cell, []).append(row)
        _grid.update(grid)
        _airports = rows
        return _airports


def loaded() -> int:
    return len(_load())


def _candidates(lat: float, lon: float, radius_nm: float) -> list:
    """Airports in the grid cells a circle of this radius can touch.

    One degree of latitude is 60nm; longitude shrinks with the cosine, so the
    cell span widens as you go north. Coarse on purpose - this only has to
    avoid scanning six thousand airports, not be exact.
    """
    _load()
    span_lat = radius_nm / 60.0
    scale = max(0.15, math.cos(math.radians(lat)))
    span_lon = radius_nm / (60.0 * scale)
    out = []
    lat0, lat1 = int(math.floor(lat - span_lat)), int(math.floor(lat + span_lat))
    lon0, lon1 = int(math.floor(lon - span_lon)), int(math.floor(lon + span_lon))
    for a in range(lat0, lat1 + 1):
        for o in range(lon0, lon1 + 1):
            out.extend(_grid.get((a, o), ()))
    return out


def as_dict(row: list, extra: dict | None = None) -> dict:
    out = {
        "icao": row[I_ICAO] or None,
        "iata": row[I_IATA] or None,
        "name": row[I_NAME] or None,
        "location": row[I_CITY] or None,
        "lat": row[I_LAT],
        "lon": row[I_LON],
    }
    if extra:
        out.update(extra)
    return out


def nearest(lat, lon, radius_nm: float = 30.0, scheduled_only: bool = False):
    """The closest airport to a point, biased towards the ones aeroplanes use.

    A large field 4nm away beats a private strip at 2nm: when an airliner is
    on the ground somewhere, it is overwhelmingly the big one.
    """
    if lat is None or lon is None:
        return None
    best = None
    for row in _candidates(lat, lon, radius_nm):
        if scheduled_only and not row[I_SCHED]:
            continue
        d = haversine_nm(lat, lon, row[I_LAT], row[I_LON])
        if d > radius_nm:
            continue
        # Effective distance: a rank of 0/1/2 shaves the bigger fields ahead.
        score = d + row[I_RANK] * 2.5
        if best is None or score < best[0]:
            best = (score, d, row)
    if best is None:
        return None
    return as_dict(best[2], {"distanceNm": round(best[1], 1)})


# --------------------------------------------------------------- destination

def _vertical_rate(ac: dict):
    for key in ("baro_rate", "geom_rate", "vert_rate"):
        v = ac.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _altitude(ac: dict):
    alt = ac.get("alt_baro")
    if alt == "ground":
        return 0.0
    if isinstance(alt, (int, float)):
        return float(alt)
    alt = ac.get("alt_geom")
    return float(alt) if isinstance(alt, (int, float)) else None


def descent_profile(ac: dict) -> dict:
    """What the aircraft is doing vertically, and whether it intends to keep
    doing it.

    Instantaneous vertical rate alone is jumpy: an arrival levels off for a
    step-down and briefly reads as cruise. The autopilot's selected altitude
    does not flicker, so when the crew has dialled in something well below
    the current level the aircraft is still on its way down whatever the
    variometer says this second.
    """
    alt = _altitude(ac)
    rate = _vertical_rate(ac)
    mcp = ac.get("nav_altitude_mcp")
    mcp = float(mcp) if isinstance(mcp, (int, float)) else None

    phase = "cruise"
    if alt is not None and alt <= 0:
        phase = "ground"
    elif rate is not None and rate <= -250:
        phase = "descent"
    elif rate is not None and rate >= 400:
        phase = "climb"
    elif (
        alt is not None and mcp is not None
        and mcp < alt - 2000
        # Only down low. A step change in the flight levels is routine cruise
        # housekeeping, and reading it as an arrival puts an aeroplane at
        # 37,000 ft on approach to a field two hundred miles away.
        and alt <= STEPDOWN_CEILING_FT
    ):
        phase = "descent"

    return {"phase": phase, "altitude": alt, "rate": rate, "selected": mcp}


def infer_destination(ac: dict) -> dict | None:
    """The airport this aircraft appears to be descending towards.

    Time-to-ground at the current rate, multiplied by the current groundspeed,
    gives a distance; laying that along the current track gives a patch of
    ground. If exactly one airport sits under it and the aircraft is pointing
    at it rather than past it, that is where it is going.

    Returns None rather than a weak guess. At cruise there is nothing here
    worth saying: the aeroplane looks identical whether it is stopping at the
    next field or carrying on for another two thousand miles.
    """
    lat, lon = ac.get("lat"), ac.get("lon")
    track = ac.get("track")
    gs = ac.get("gs")
    if lat is None or lon is None or track is None or not gs:
        return None

    profile = descent_profile(ac)
    alt, rate = profile["altitude"], profile["rate"]
    if profile["phase"] != "descent" or alt is None or alt <= 0:
        return None

    # A level step-down still reads as a descent, but it has no rate to work
    # with. Fall back to a typical arrival gradient so the estimate survives.
    fpm = abs(rate) if rate and rate <= -250 else 1500.0
    minutes = alt / fpm
    dist = float(gs) * minutes / 60.0
    if dist > MAX_PROJECTION_NM:
        return None

    aim_lat, aim_lon = project(lat, lon, float(track), dist)

    best = None
    for row in _candidates(aim_lat, aim_lon, AIM_RADIUS_NM):
        if not row[I_SCHED] and row[I_RANK] > 1:
            continue
        off_aim = haversine_nm(aim_lat, aim_lon, row[I_LAT], row[I_LON])
        if off_aim > AIM_RADIUS_NM:
            continue
        # Reject anything behind the aircraft: the projection can overshoot,
        # and a field already passed is not a destination.
        to_field = bearing_deg(lat, lon, row[I_LAT], row[I_LON])
        off_track = angle_between(float(track), to_field)
        if off_track > AHEAD_CONE_DEG:
            continue
        score = off_aim + off_track * 0.25 + row[I_RANK] * 3.0
        if best is None or score < best[0]:
            best = (score, off_aim, off_track, row)

    if best is None:
        return None

    _, off_aim, off_track, row = best
    remaining = haversine_nm(lat, lon, row[I_LAT], row[I_LON])

    # How much to trust it. Low and close is nearly certain. High and far is
    # only a direction of travel with an airport somewhere in it, which is not
    # worth saying out loud, so it is dropped rather than hedged.
    if alt <= 10000 and remaining <= 40:
        confidence = "high"
    elif alt <= 20000 and remaining <= 120:
        confidence = "medium"
    else:
        return None

    return as_dict(row, {
        "distanceNm": round(remaining, 1),
        "confidence": confidence,
        "offTrackDeg": round(off_track),
        "source": "descent",
    })


# -------------------------------------------------------------------- origin

def _fetch_trace(hex_id: str):
    url = TRACE_URL.format(suffix=hex_id[-2:], hex=hex_id)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    })
    # Short, because the board is waiting on this response. A slow trace is
    # worth abandoning: the destination and the route are already on their way
    # and the origin can fill in on the next refresh.
    with urllib.request.urlopen(req, timeout=6.0) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8", "replace"))


def origin_from_trace(hex_id: str) -> dict | None:
    """Where this aircraft most recently left the ground.

    The trace covers a full day and usually holds several flights, so it is
    walked backwards to the last point the aircraft was on the ground. What
    follows that point is the current leg, and the field beside it is where
    this flight began - observed, not filed.
    """
    hex_id = (hex_id or "").strip().lower()
    if len(hex_id) < 3:
        return None

    now = time.time()
    with _trace_lock:
        hit = _trace_cache.get(hex_id)
        if hit and hit[0] > now:
            return hit[1]

    try:
        data = _fetch_trace(hex_id)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, gzip.BadGzipFile):
        return None  # transient; do not cache a failure as an answer

    result = _origin_from_points(data.get("trace") or [])
    with _trace_lock:
        _trace_cache[hex_id] = (now + TRACE_TTL, result)
        if len(_trace_cache) > 512:
            for key in sorted(_trace_cache, key=lambda k: _trace_cache[k][0])[:128]:
                _trace_cache.pop(key, None)
    return result


def _origin_from_points(points: list) -> dict | None:
    """Departure airport for the leg still in progress.

    A point counts as on-the-ground when the feed says so outright, or when
    the aircraft is low and slow enough that it cannot be flying. Traces have
    gaps, so a long silence also ends a leg: an aircraft that vanishes for an
    hour landed somewhere the receivers could not see it.
    """
    if not points:
        return None

    GAP_S = 45 * 60

    start = 0
    for i in range(len(points) - 1, 0, -1):
        p = points[i]
        alt = p[3] if len(p) > 3 else None
        gs = p[4] if len(p) > 4 else None
        on_ground = alt == "ground" or (
            isinstance(alt, (int, float)) and alt < 500
            and isinstance(gs, (int, float)) and gs < 80
        )
        gap = (points[i][0] - points[i - 1][0]) > GAP_S
        if on_ground or gap:
            start = i
            break

    # Walk forward from the break to the first point with a usable fix.
    for p in points[start:]:
        if len(p) > 2 and isinstance(p[1], (int, float)) and isinstance(p[2], (int, float)):
            found = nearest(p[1], p[2], radius_nm=12.0)
            if found:
                found["source"] = "trace"
                found["confidence"] = "high" if start > 0 else "medium"
                return found
            return None
    return None
