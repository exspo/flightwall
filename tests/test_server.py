#!/usr/bin/env python3
"""Server tests. Stdlib unittest, no network.

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import flightwall  # noqa: E402

ORD = (41.9742, -87.9073)

SAMPLE = {
    "ac": [
        {
            "hex": "a1b2c3", "flight": "UAL2402 ", "r": "N37502", "t": "A21N",
            "desc": "AIRBUS A-321neo", "category": "A3", "lat": 41.99, "lon": -87.85,
            "alt_baro": 4100, "gs": 217, "track": 263.4, "baro_rate": -1088,
            "squawk": "3413", "dst": 9.4, "dir": 71.2, "seen_pos": 0.4,
        },
        {   # Ground vehicle: has no altitude, and must not read as an aircraft.
            "hex": "ffffff", "flight": "OPS12 ", "category": "C2",
            "lat": 41.975, "lon": -87.906, "alt_baro": "ground", "gs": 12,
        },
        {   # Helicopter identified only by type code, category missing.
            "hex": "abc123", "flight": "N911MD ", "t": "EC35", "lat": 42.05,
            "lon": -87.80, "alt_baro": 1200, "gs": 110, "track": 15, "geom_rate": 300,
        },
        {   # Military flag via dbFlags bit 0.
            "hex": "ae1234", "flight": "RCH471 ", "t": "C17", "category": "A5",
            "lat": 42.2, "lon": -88.1, "alt_baro": 22000, "gs": 380, "dbFlags": 1,
        },
        {   # No position at all: must be dropped, not rendered at 0,0.
            "hex": "deadbe", "flight": "GHOST1 ", "alt_baro": 30000,
        },
        {   # Emergency squawk with no explicit emergency field.
            "hex": "c0ffee", "flight": "N44RB ", "t": "SR22", "category": "A1",
            "lat": 41.90, "lon": -87.95, "alt_baro": 3000, "squawk": "7700",
        },
    ],
    "now": 1_700_000_000_000,
}


class NormalizeTests(unittest.TestCase):
    def rows(self):
        return {
            r["hex"]: r
            for r in (flightwall.normalize(ac, *ORD) for ac in SAMPLE["ac"])
            if r
        }

    def test_drops_aircraft_without_a_position(self):
        self.assertNotIn("deadbe", self.rows())

    def test_flattens_an_airliner(self):
        ac = self.rows()["a1b2c3"]
        self.assertEqual(ac["flight"], "UAL2402")  # trailing pad stripped
        self.assertEqual(ac["class"], "commercial")
        self.assertEqual(ac["alt"], 4100)
        self.assertEqual(ac["vs"], -1088)
        self.assertEqual(ac["dst"], 9.4)
        self.assertFalse(ac["gnd"])

    def test_ground_vehicle_is_flagged_not_flown(self):
        ac = self.rows()["ffffff"]
        self.assertTrue(ac["gnd"])
        self.assertIsNone(ac["alt"])
        self.assertEqual(ac["class"], "ground")

    def test_helicopter_detected_by_type_code(self):
        ac = self.rows()["abc123"]
        self.assertEqual(ac["class"], "helicopter")
        self.assertEqual(ac["vs"], 300)  # falls back to geom_rate

    def test_military_flag(self):
        ac = self.rows()["ae1234"]
        self.assertEqual(ac["class"], "military")
        self.assertTrue(ac["mil"])

    def test_emergency_inferred_from_squawk(self):
        ac = self.rows()["c0ffee"]
        self.assertEqual(ac["emergency"], "EMERGENCY")
        self.assertEqual(ac["class"], "general")

    def test_distance_computed_when_upstream_omits_it(self):
        ac = flightwall.normalize(
            {"hex": "x", "lat": 42.9742, "lon": -87.9073}, *ORD
        )
        # One degree of latitude is 60 nautical miles, due north.
        self.assertAlmostEqual(ac["dst"], 60.0, delta=0.5)
        self.assertAlmostEqual(ac["dir"], 0.0, delta=0.5)


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        flightwall.aircraft_cache._data.clear()
        self._real = flightwall.fetch_json

    def tearDown(self):
        flightwall.fetch_json = self._real

    def test_falls_through_to_the_next_provider(self):
        def flaky(url, payload=None, timeout=8.0):
            self.calls.append(url)
            if "adsb.lol" in url:
                raise OSError("connection refused")
            return SAMPLE

        flightwall.fetch_json = flaky
        result = flightwall.get_aircraft(*ORD, 60)
        self.assertEqual(result["source"], "adsb.fi")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(result["aircraft"]), 5)  # the position-less one is gone

    def test_results_are_sorted_by_distance(self):
        flightwall.fetch_json = lambda *a, **k: SAMPLE
        distances = [ac["dst"] for ac in flightwall.get_aircraft(*ORD, 60)["aircraft"]]
        self.assertEqual(distances, sorted(distances))

    def test_all_providers_down_raises(self):
        def dead(*a, **k):
            raise OSError("no route to host")

        flightwall.fetch_json = dead
        with self.assertRaises(RuntimeError):
            flightwall.get_aircraft(*ORD, 60)

    def test_second_call_is_served_from_cache(self):
        calls = []

        def counting(*a, **k):
            calls.append(1)
            return SAMPLE

        flightwall.fetch_json = counting
        flightwall.get_aircraft(*ORD, 60)
        # Small GPS jitter inside the same ~1km cache bucket must not trigger
        # a second upstream call. Crossing a bucket boundary legitimately does.
        again = flightwall.get_aircraft(ORD[0] - 0.0004, ORD[1] + 0.0004, 60)
        self.assertEqual(len(calls), 1)
        self.assertTrue(again["cached"])


class RouteTests(unittest.TestCase):
    def setUp(self):
        flightwall.route_cache._data.clear()
        self._real = flightwall.fetch_json

    def tearDown(self):
        flightwall.fetch_json = self._real

    def test_plausible_route_is_returned_and_cached(self):
        calls = []

        def fake(url, payload=None, timeout=8.0):
            calls.append(payload)
            return [{
                "callsign": "UAL2402", "airline_code": "UAL", "number": "2402",
                "_airport_codes_iata": "ORD-LAX", "plausible": 1,
                "_airports": [
                    {"iata": "ORD", "icao": "KORD", "location": "Chicago"},
                    {"iata": "LAX", "icao": "KLAX", "location": "Los Angeles"},
                ],
            }]

        flightwall.fetch_json = fake
        out = flightwall.get_routes([{"callsign": "UAL2402", "lat": 41.9, "lng": -87.9}])
        self.assertEqual(out["routes"]["UAL2402"]["route"], "ORD-LAX")
        self.assertEqual(out["errors"], [])

        flightwall.get_routes([{"callsign": "UAL2402", "lat": 41.9, "lng": -87.9}])
        self.assertEqual(len(calls), 1, "second lookup should hit the cache")

    def test_implausible_route_is_discarded(self):
        # A wrong route on the board is worse than a blank one.
        flightwall.fetch_json = lambda *a, **k: [
            {"callsign": "XYZ1", "_airport_codes_iata": "AAA-BBB", "plausible": 0}
        ]
        out = flightwall.get_routes([{"callsign": "XYZ1", "lat": 0, "lng": 0}])
        self.assertIsNone(out["routes"]["XYZ1"])

    def test_unanswered_callsign_is_negative_cached(self):
        flightwall.fetch_json = lambda *a, **k: []
        out = flightwall.get_routes([{"callsign": "NOPE1", "lat": 0, "lng": 0}])
        self.assertIsNone(out["routes"]["NOPE1"])
        self.assertIn("NOPE1", flightwall.route_cache._data)

    def test_upstream_failure_is_reported_not_swallowed(self):
        # The client cannot distinguish "still looking" from "lookup broke"
        # unless the failure comes back, which stranded the UI on a spinner.
        def dead(*a, **k):
            raise OSError("connection refused")

        flightwall.fetch_json = dead
        out = flightwall.get_routes([{"callsign": "DAL926", "lat": 41.9, "lng": -87.9}])
        self.assertEqual(out["routes"], {})
        self.assertTrue(out["errors"])
        self.assertIn("connection refused", out["errors"][0])
        self.assertNotIn("DAL926", flightwall.route_cache._data,
                         "a failed lookup must not be cached as a known miss")

    def test_falls_back_to_adsbdb_when_the_batch_endpoint_misbehaves(self):
        # The reported failure: adsb.lol answered 200 with a non-JSON body.
        calls = []

        def fake(url, payload=None, timeout=8.0):
            calls.append(url)
            if "adsb.lol" in url:
                raise ValueError("HTTP 200 returned non-JSON: <empty body>")
            return {
                "response": {
                    "flightroute": {
                        "callsign": "DAL926",
                        "callsign_iata": "DL926",
                        "airline": {"icao": "DAL"},
                        "origin": {
                            "iata_code": "ATL", "icao_code": "KATL",
                            "name": "Hartsfield Jackson Atlanta International",
                            "municipality": "Atlanta", "country_iso_name": "US",
                        },
                        "destination": {
                            "iata_code": "MSP", "icao_code": "KMSP",
                            "name": "Minneapolis St Paul International",
                            "municipality": "Minneapolis", "country_iso_name": "US",
                        },
                    }
                }
            }

        flightwall.fetch_json = fake
        out = flightwall.get_routes([{"callsign": "DAL926", "lat": 41.9, "lng": -87.9}])
        entry = out["routes"]["DAL926"]
        self.assertEqual(entry["route"], "ATL-MSP")
        self.assertEqual(entry["airports"][0]["location"], "Atlanta")
        self.assertEqual(entry["airports"][-1]["iata"], "MSP")
        self.assertTrue(out["errors"], "the first provider's failure should still be reported")
        self.assertTrue(any("adsbdb" in c for c in calls))

    def test_adsbdb_404_is_a_definitive_miss(self):
        def fake(url, payload=None, timeout=8.0):
            if "adsb.lol" in url:
                raise OSError("down")
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        flightwall.fetch_json = fake
        out = flightwall.get_routes([{"callsign": "ZZZ999", "lat": 0, "lng": 0}])
        self.assertIsNone(out["routes"]["ZZZ999"])

    def test_total_failure_does_not_negative_cache(self):
        # Caching a miss here would hide the callsign behind "no route on file"
        # for half an hour after the provider recovered.
        def dead(*a, **k):
            raise OSError("no route to host")

        flightwall.fetch_json = dead
        flightwall.get_routes([{"callsign": "DAL926", "lat": 0, "lng": 0}])
        self.assertNotIn("DAL926", flightwall.route_cache._data)

    def test_non_list_response_is_reported(self):
        flightwall.fetch_json = lambda *a, **k: {"error": "rate limited"}
        out = flightwall.get_routes([{"callsign": "DAL926", "lat": 0, "lng": 0}])
        self.assertTrue(out["errors"])


class HttpTests(unittest.TestCase):
    """Exercise the real handler over a real socket."""

    @classmethod
    def setUpClass(cls):
        flightwall.fetch_json = lambda *a, **k: SAMPLE
        flightwall.Handler.token = "test-token"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), flightwall.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def setUp(self):
        flightwall.aircraft_cache._data.clear()

    def test_health_needs_no_token(self):
        status, body, _ = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_api_rejects_a_missing_token(self):
        self.assertEqual(self.get("/api/aircraft?lat=41.9&lon=-87.9")[0], 401)

    def test_api_rejects_a_wrong_token(self):
        self.assertEqual(self.get("/api/aircraft?lat=41.9&lon=-87.9&k=nope")[0], 401)

    def test_api_accepts_the_token_in_the_query(self):
        status, body, _ = self.get("/api/aircraft?lat=41.9742&lon=-87.9073&k=test-token")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["aircraft"]), 5)

    def test_api_accepts_the_token_in_a_cookie(self):
        status, _, _ = self.get(
            "/api/aircraft?lat=41.9&lon=-87.9", headers={"Cookie": "fw_token=test-token"}
        )
        self.assertEqual(status, 200)

    def test_index_hands_back_a_cookie_so_the_url_can_be_cleaned(self):
        status, _, headers = self.get("/?k=test-token")
        self.assertEqual(status, 200)
        self.assertIn("fw_token=test-token", headers.get("Set-Cookie", ""))

    def test_missing_coordinates_are_a_client_error(self):
        self.assertEqual(self.get("/api/aircraft?k=test-token")[0], 400)

    def test_out_of_range_coordinates_rejected(self):
        self.assertEqual(self.get("/api/aircraft?lat=99&lon=0&k=test-token")[0], 400)

    def test_radius_is_clamped_rather_than_trusted(self):
        status, body, _ = self.get("/api/aircraft?lat=41.9&lon=-87.9&radius=99999&k=test-token")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["center"]["radius"], flightwall.MAX_RADIUS_NM)

    def test_path_traversal_is_refused(self):
        status, _, _ = self.get("/../server/flightwall.py?k=test-token")
        self.assertIn(status, (400, 404))

    def test_landmarks_are_clipped_to_the_view(self):
        status, body, _ = self.get("/api/landmarks?lat=41.9742&lon=-87.9073&radius=60&k=test-token")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["cities"], "expected towns near O'Hare")
        # Nearest first: the client draws in order and drops colliding labels.
        distances = [c[2] for c in data["cities"]]
        self.assertEqual(distances, sorted(distances))
        # Nothing may be reported beyond the clip boundary.
        limit = 60 * 1.25
        for runs in data["lines"].values():
            for run in runs:
                self.assertLessEqual(max(run[0::2]), limit + 0.01)

    def test_landmarks_drop_counties_at_wide_range(self):
        _, body, _ = self.get("/api/landmarks?lat=41.9742&lon=-87.9073&radius=250&k=test-token")
        self.assertEqual(json.loads(body)["lines"]["counties"], [])

    def test_landmarks_reject_bad_coordinates(self):
        self.assertEqual(self.get("/api/landmarks?k=test-token")[0], 400)
        self.assertEqual(self.get("/api/landmarks?lat=99&lon=0&k=test-token")[0], 400)

    def test_landmarks_need_the_token(self):
        self.assertEqual(self.get("/api/landmarks?lat=41&lon=-87")[0], 401)

    def test_static_assets_are_served(self):
        for path in ("/index.html", "/app.js", "/led.js", "/styles.css",
                     "/manifest.webmanifest", "/data/airlines.json", "/icons/icon-180.png"):
            with self.subTest(path=path):
                self.assertEqual(self.get(f"{path}?k=test-token")[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
