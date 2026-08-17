#!/usr/bin/env python3
"""Build the airport dataset used to name where an aircraft came from and is
going.

Downloads OurAirports (public domain), trims it hard, and writes a compact
file into server/data/. Run once; the output is committed, so the laptop
never needs to fetch anything.

    python3 scripts/build_airports.py

Only airports an airliner would actually use are kept: large and medium
fields, plus small ones that hold scheduled service. Heliports, seaplane
bases and the long tail of private strips are dropped, because naming a
destination is only useful when the answer is somewhere a passenger flight
lands.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "server" / "data"

SOURCE = "https://davidmegginson.github.io/ourairports-data/airports.csv"

# Rank matters when two fields are equally close: a jet on approach is far
# more likely bound for the international airport than the county strip
# beside it.
RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}


def fetch(url: str) -> bytes:
    print(f"  fetching {url.rsplit('/', 1)[-1]}")
    req = urllib.request.Request(url, headers={"User-Agent": "FlightWall/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def build() -> None:
    raw = fetch(SOURCE).decode("utf-8", "replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    print(f"  {len(rows)} airports in source")

    out = []
    for row in rows:
        kind = row.get("type") or ""
        if kind not in RANK:
            continue
        scheduled = (row.get("scheduled_service") or "").lower() == "yes"
        # A small field earns its place only by having scheduled service.
        if kind == "small_airport" and not scheduled:
            continue
        try:
            lat = round(float(row["latitude_deg"]), 4)
            lon = round(float(row["longitude_deg"]), 4)
        except (KeyError, TypeError, ValueError):
            continue

        icao = (row.get("icao_code") or row.get("gps_code") or "").strip().upper()
        iata = (row.get("iata_code") or "").strip().upper()
        if not icao and not iata:
            continue

        out.append([
            icao,
            iata,
            (row.get("name") or "").strip(),
            (row.get("municipality") or "").strip(),
            lat,
            lon,
            RANK[kind],
            1 if scheduled else 0,
        ])

    # Biggest first. The nearest-airport search walks this in order and can
    # stop early once it has a large field within its radius.
    out.sort(key=lambda a: (a[6], -a[7], a[0]))

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "airports.json"
    path.write_text(json.dumps(out, separators=(",", ":")))
    kb = path.stat().st_size / 1024
    big = sum(1 for a in out if a[6] == 0)
    mid = sum(1 for a in out if a[6] == 1)
    small = sum(1 for a in out if a[6] == 2)
    print(f"  wrote {path.relative_to(ROOT)}  {len(out)} airports, {kb:.0f} KB")
    print(f"    {big} large, {mid} medium, {small} small-with-service")


if __name__ == "__main__":
    build()
