"""Ground features clipped to whatever the radar is currently showing.

The full dataset is a couple of megabytes and lives here on the laptop. The
phone asks for one view at a time and gets a few kilobytes back, already
converted to the distance-and-bearing form the scope draws in - so the client
needs no projection maths and no map library.

Data credits: county boundaries from the US Census (public domain), state and
province lines and lakes from Natural Earth (public domain), city list from
kelvins/US-Cities-Database (MIT).
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

# Keep line vertices a little beyond the rim so features run off the edge of
# the scope instead of stopping short of it.
CLIP_MARGIN = 1.25
MAX_CITIES = 400

# Beyond this range county boundaries stop being orientation and start being
# noise - several hundred rings crammed into a 300 px scope. State lines and
# coastline carry the orientation at wide ranges instead.
COUNTY_MAX_RADIUS_NM = 100

_lock = threading.Lock()
_loaded = False
_layers: dict = {"counties": [], "states": [], "lakes": []}
_cities: list = []


def load() -> None:
    """Read the dataset once, on first use, so startup stays instant."""
    global _loaded
    with _lock:
        if _loaded:
            return
        for name in _layers:
            path = DATA / f"{name}.json"
            try:
                _layers[name] = json.loads(path.read_text())
            except (OSError, ValueError):
                _layers[name] = []  # a missing layer just does not draw
        try:
            _cities.extend(json.loads((DATA / "cities.json").read_text()))
        except (OSError, ValueError):
            pass
        _loaded = True


def available() -> bool:
    load()
    return bool(_cities or any(_layers.values()))


def _haversine_nm(lat1, lon1, lat2, lon2) -> float:
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _view_box(lat: float, lon: float, radius_nm: float):
    """A generous lat/lon box around the view, for cheap rejection before any
    great-circle maths. Degenerate near the poles, which North America is not."""
    span = (radius_nm * CLIP_MARGIN) / 60.0
    cos_lat = max(0.08, math.cos(math.radians(lat)))
    return (lat - span, lon - span / cos_lat, lat + span, lon + span / cos_lat)


def _project(clat: float, clon: float, lat: float, lon: float):
    """Local flat projection in nautical miles, x east and y north.

    Over a few hundred miles this is within a fraction of a percent of the
    great circle, and it turns clipping into plain 2D geometry.
    """
    return ((lon - clon) * 60.0 * math.cos(math.radians(clat)), (lat - clat) * 60.0)


def _polar(x: float, y: float):
    return (round(math.hypot(x, y), 2), round((math.degrees(math.atan2(x, y)) + 360.0) % 360.0, 1))


def _segment_in_circle(x0, y0, x1, y1, r):
    """The sub-span of a segment lying inside a circle at the origin, as
    (t0, t1) along the segment, or None.

    Testing endpoints alone is not enough: a straight state border can have
    two vertices hundreds of miles apart with the entire view between them,
    and that segment must still be drawn.
    """
    dx, dy = x1 - x0, y1 - y0
    a = dx * dx + dy * dy
    if a == 0:
        return (0.0, 0.0) if math.hypot(x0, y0) <= r else None
    b = 2 * (x0 * dx + y0 * dy)
    c = x0 * x0 + y0 * y0 - r * r
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    root = math.sqrt(disc)
    t0 = max(0.0, (-b - root) / (2 * a))
    t1 = min(1.0, (-b + root) / (2 * a))
    return (t0, t1) if t0 <= t1 else None


def _clip_ring(flat: list, lat: float, lon: float, limit: float) -> list:
    """Split one ring into the runs of it that fall inside the view, cut
    exactly at the boundary so no vertex is ever reported beyond it."""
    points = [
        _project(lat, lon, flat[i], flat[i + 1])
        for i in range(0, len(flat) - 1, 2)
    ]
    if len(points) < 2:
        return []

    runs = []
    current = []

    for i in range(len(points) - 1):
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        span = _segment_in_circle(x0, y0, x1, y1, limit)
        if span is None:
            if len(current) >= 2:
                runs.append(current)
            current = []
            continue

        t0, t1 = span
        start = _polar(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0)
        end = _polar(x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)

        if current and t0 == 0.0:
            current.append(end)  # continues straight on from the last vertex
        else:
            if len(current) >= 2:
                runs.append(current)
            current = [start, end]

        if t1 < 1.0:  # leaves the circle part way along this segment
            if len(current) >= 2:
                runs.append(current)
            current = []

    if len(current) >= 2:
        runs.append(current)

    # Flatten to [d, b, d, b, ...].
    return [[v for pair in run for v in pair] for run in runs]


def query(lat: float, lon: float, radius_nm: float) -> dict:
    load()
    min_lat, min_lon, max_lat, max_lon = _view_box(lat, lon, radius_nm)
    limit = radius_nm * CLIP_MARGIN

    lines = {}
    for name, rings in _layers.items():
        out = []
        if name == "counties" and radius_nm > COUNTY_MAX_RADIUS_NM:
            lines[name] = out
            continue
        for ring in rings:
            b = ring["b"]
            if b[2] < min_lat or b[0] > max_lat or b[3] < min_lon or b[1] > max_lon:
                continue
            out.extend(_clip_ring(ring["p"], lat, lon, limit))
        lines[name] = out

    cities = []
    for name, state, city_lat, city_lon in _cities:
        if not (min_lat <= city_lat <= max_lat and min_lon <= city_lon <= max_lon):
            continue
        distance = _haversine_nm(lat, lon, city_lat, city_lon)
        if distance > radius_nm:
            continue
        cities.append([name, state, round(distance, 2), round(_bearing_deg(lat, lon, city_lat, city_lon), 1)])

    # Nearest first: the client draws in order and drops labels that collide,
    # so ordering here decides what survives when the view is crowded.
    cities.sort(key=lambda c: c[2])

    return {
        "center": {"lat": lat, "lon": lon, "radius": radius_nm},
        "lines": lines,
        "cities": cities[:MAX_CITIES],
        "truncated": len(cities) > MAX_CITIES,
    }
