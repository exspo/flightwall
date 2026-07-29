#!/usr/bin/env python3
"""Build the landmark dataset the radar draws under the aircraft.

Downloads public-domain / MIT geographic data, trims it hard, and writes
compact files into server/data/. Run once; the outputs are committed, so the
laptop never needs to fetch anything.

    python3 scripts/build_landmarks.py

Coordinates are stored as flat [lat, lon, lat, lon, ...] arrays rounded to
four decimals (about 11 m), which is far finer than a 300 px radar scope can
resolve and roughly halves the file size versus full precision.
"""

from __future__ import annotations

import json
import math
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "server" / "data"

SOURCES = {
    # US Census cartographic boundaries, public domain, mirrored by plotly.
    "counties": "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json",
    # Natural Earth, public domain.
    "states": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces_lines.geojson",
    "lakes": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_lakes.geojson",
    # kelvins/US-Cities-Database, MIT.
    "cities": "https://raw.githubusercontent.com/kelvins/US-Cities-Database/main/csv/us_cities.csv",
}

# Simplification tolerance in degrees. 0.004 is roughly 400 m, which is under
# one pixel at any radar range this app offers.
EPSILON = 0.004
LAKE_MIN_SCALERANK = 3  # 0-2 keeps the genuinely large lakes


def fetch(url: str) -> bytes:
    print(f"  fetching {url.rsplit('/', 1)[-1]}")
    req = urllib.request.Request(url, headers={"User-Agent": "FlightWall/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def perpendicular_distance(point, start, end) -> float:
    (x, y), (x1, y1), (x2, y2) = point, start, end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def simplify(points: list, epsilon: float) -> list:
    """Iterative Douglas-Peucker. Iterative rather than recursive because some
    county rings are deep enough to blow the default recursion limit."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        worst, index = 0.0, -1
        for i in range(first + 1, last):
            d = perpendicular_distance(points[i], points[first], points[last])
            if d > worst:
                worst, index = d, i
        if index != -1 and worst > epsilon:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep) if k]


def rings_of(geometry: dict) -> list:
    """Every coordinate ring in a Polygon or MultiPolygon, as (lon, lat)."""
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return list(coords)
    if kind == "MultiPolygon":
        return [ring for polygon in coords for ring in polygon]
    if kind == "LineString":
        return [coords]
    if kind == "MultiLineString":
        return list(coords)
    return []


def encode(points: list) -> dict | None:
    """A simplified ring plus its bounding box, so the server can cull by box
    before touching any vertex."""
    simplified = simplify([(lon, lat) for lon, lat in points], EPSILON)
    if len(simplified) < 2:
        return None
    lats = [lat for _, lat in simplified]
    lons = [lon for lon, _ in simplified]
    flat = []
    for lon, lat in simplified:
        flat.append(round(lat, 4))
        flat.append(round(lon, 4))
    return {
        "b": [round(min(lats), 4), round(min(lons), 4), round(max(lats), 4), round(max(lons), 4)],
        "p": flat,
    }


def build_lines(raw: bytes, keep) -> list:
    data = json.loads(raw)
    out = []
    for feature in data.get("features", []):
        # Natural Earth is inconsistent about property case between files, so
        # normalise before matching rather than guessing per source.
        props = {k.lower(): v for k, v in (feature.get("properties") or {}).items()}
        if not keep(props):
            continue
        for ring in rings_of(feature.get("geometry") or {}):
            if not ring or not isinstance(ring[0], (list, tuple)) or len(ring[0]) < 2:
                continue
            encoded = encode(ring)
            if encoded:
                out.append(encoded)
    return out


def build_cities(raw: bytes) -> list:
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    cities = []
    for row in rows:
        try:
            lat = round(float(row["LATITUDE"]), 4)
            lon = round(float(row["LONGITUDE"]), 4)
        except (KeyError, TypeError, ValueError):
            continue
        name = (row.get("CITY") or "").strip()
        if not name:
            continue
        cities.append([name, (row.get("STATE_CODE") or "").strip(), lat, lon])
    cities.sort(key=lambda c: (c[1], c[0]))
    return cities


def write(name: str, payload) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"  wrote {path.relative_to(ROOT)}  {path.stat().st_size / 1024:.0f} KB")


def main() -> None:
    print("counties (US Census, public domain)")
    counties = build_lines(fetch(SOURCES["counties"]), lambda p: True)
    write("counties", counties)

    print("state and province lines (Natural Earth, public domain)")
    wanted = {"USA", "CAN", "MEX"}
    states = build_lines(fetch(SOURCES["states"]), lambda p: p.get("adm0_a3") in wanted)
    if not states:
        raise SystemExit("no state lines matched - the source property names changed")
    write("states", states)

    print("major lakes (Natural Earth, public domain)")
    lakes = build_lines(
        fetch(SOURCES["lakes"]),
        lambda p: (p.get("scalerank") is not None and p["scalerank"] < LAKE_MIN_SCALERANK),
    )
    write("lakes", lakes)

    print("cities (kelvins/US-Cities-Database, MIT)")
    cities = build_cities(fetch(SOURCES["cities"]))
    write("cities", cities)

    print(f"\ndone: {len(counties)} county rings, {len(states)} state lines, "
          f"{len(lakes)} lake rings, {len(cities)} cities")


if __name__ == "__main__":
    main()
