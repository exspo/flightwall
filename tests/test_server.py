#!/usr/bin/env python3
"""Server tests. Stdlib unittest, no network.

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import flightwall  # noqa: E402
import landmarks  # noqa: E402

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
        flightwall.note_preferred(None)  # provider preference is global state
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

    @staticmethod
    def _adsbdb(callsign="DAL926", origin=("ATL", "KATL", "Atlanta", 33.6367, -84.4281),
                dest=("MSP", "KMSP", "Minneapolis", 44.8820, -93.2218)):
        def node(spec):
            iata, icao, city, lat, lon = spec
            return {
                "iata_code": iata, "icao_code": icao, "name": f"{city} International",
                "municipality": city, "country_iso_name": "US",
                "latitude": lat, "longitude": lon,
            }
        return {"response": {"flightroute": {
            "callsign": callsign, "callsign_iata": callsign,
            "airline": {"icao": callsign[:3]},
            "origin": node(origin), "destination": node(dest),
        }}}

    def test_plausible_route_is_returned_and_cached(self):
        calls = []

        def fake(url, payload=None, timeout=8.0):
            calls.append(url)
            return self._adsbdb("DAL926")

        flightwall.fetch_json = fake
        # Somewhere between Atlanta and Minneapolis.
        out = flightwall.get_routes([{"callsign": "DAL926", "lat": 39.0, "lng": -88.8}])
        self.assertEqual(out["routes"]["DAL926"]["route"], "ATL-MSP")
        self.assertEqual(out["errors"], [])

        flightwall.get_routes([{"callsign": "DAL926", "lat": 39.0, "lng": -88.8}])
        self.assertEqual(len(calls), 1, "second lookup should hit the cache")

    def test_route_contradicted_by_position_is_flagged_suspect(self):
        # The failure that made the badge useless: a table answers confidently
        # with a route the aircraft is thousands of miles from.
        flightwall.fetch_json = lambda *a, **k: self._adsbdb("SWA1195")
        out = flightwall.get_routes([{"callsign": "SWA1195", "lat": 38.0, "lng": -78.5}])
        self.assertTrue(out["routes"]["SWA1195"]["suspect"])

    def test_unanswered_callsign_is_negative_cached(self):
        flightwall.fetch_json = lambda *a, **k: {"response": "unknown callsign"}
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

    def test_falls_back_to_the_standing_data_when_adsbdb_misbehaves(self):
        calls = []

        def fake(url, payload=None, timeout=8.0):
            calls.append(url)
            if "adsbdb" in url:
                raise ValueError("HTTP 200 returned non-JSON: <empty body>")
            return {
                "callsign": "DAL926", "airline_code": "DAL", "number": "926",
                "_airport_codes_iata": "ATL-MSP",
                "_airports": [
                    {"iata": "ATL", "icao": "KATL", "location": "Atlanta",
                     "lat": 33.6367, "lon": -84.4281},
                    {"iata": "MSP", "icao": "KMSP", "location": "Minneapolis",
                     "lat": 44.8820, "lon": -93.2218},
                ],
            }

        flightwall.fetch_json = fake
        out = flightwall.get_routes([{"callsign": "DAL926", "lat": 39.0, "lng": -88.8}])
        entry = out["routes"]["DAL926"]
        self.assertEqual(entry["route"], "ATL-MSP")
        self.assertEqual(entry["airports"][-1]["iata"], "MSP")
        self.assertTrue(out["errors"], "the first provider's failure should still be reported")
        self.assertTrue(any("vrs-standing-data" in c for c in calls))

    def test_adsbdb_404_is_a_definitive_miss(self):
        def fake(url, payload=None, timeout=8.0):
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


class AeroApiTests(unittest.TestCase):
    """FlightAware is the only source that knows the date, so when it answers
    it wins outright and is never second-guessed by the position check."""

    def setUp(self):
        flightwall.route_cache._data.clear()
        self._key = flightwall.aeroapi_key
        self._fetch = flightwall.fetch_json
        self._open = flightwall.urllib.request.urlopen
        self._trace = flightwall.airports.origin_from_trace
        self._state = flightwall.STATE
        self._cap = flightwall.AEROAPI_MONTHLY_CAP
        # The spend counter lives on disk. Point it somewhere disposable so a
        # test run cannot touch the real allowance record.
        self._tmp = tempfile.TemporaryDirectory()
        flightwall.STATE = Path(self._tmp.name)
        flightwall.aeroapi_key = lambda: "test-key"
        flightwall.airports.origin_from_trace = lambda h: None

    def tearDown(self):
        flightwall.aeroapi_key = self._key
        flightwall.fetch_json = self._fetch
        flightwall.urllib.request.urlopen = self._open
        flightwall.airports.origin_from_trace = self._trace
        flightwall.STATE = self._state
        flightwall.AEROAPI_MONTHLY_CAP = self._cap
        self._tmp.cleanup()

    def _serve(self, payload):
        class Resp:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        flightwall.urllib.request.urlopen = lambda *a, **k: Resp()

    def test_a_live_answer_overrides_whatever_the_tables_said(self):
        self._serve({"flights": [
            {"ident": "ENY3390", "actual_off": "2026-08-17T12:00:00Z", "actual_on": None,
             "operator_icao": "ENY", "flight_number": "3390", "progress_percent": 80,
             "status": "En Route", "origin": {"code_icao": "KCLT", "code_iata": "CLT"},
             "destination": {"code_icao": "KCHO", "code_iata": "CHO"}},
        ]})
        flightwall.fetch_json = lambda *a, **k: {"response": {"flightroute": {
            "callsign": "ENY3390", "airline": {"icao": "ENY"},
            "origin": {"iata_code": "EYW", "latitude": 24.5561, "longitude": -81.7596},
            "destination": {"iata_code": "ORD", "latitude": 41.9786, "longitude": -87.9048},
        }}}
        out = flightwall.get_routes(
            [{"callsign": "ENY3390", "lat": 38.0, "lng": -78.5}], live="ENY3390")
        entry = out["routes"]["ENY3390"]
        self.assertEqual(entry["route"], "CLT-CHO")
        self.assertEqual(entry["source"], "aeroapi")
        self.assertEqual(entry["confidence"], "confirmed")
        self.assertNotIn("suspect", entry)

    def test_cycling_past_an_aircraft_costs_nothing(self):
        # The whole point of `live`: focus moves every nine seconds and must
        # never spend a query on its own.
        def forbidden(*a, **k):
            raise AssertionError("aeroapi called without an explicit request")

        flightwall.urllib.request.urlopen = forbidden
        flightwall.airports.origin_from_trace = lambda h: None
        flightwall.fetch_json = lambda *a, **k: {"response": "unknown callsign"}
        flightwall.get_routes(
            [{"callsign": "AAA1", "lat": 38.0, "lng": -78.5, "hex": "a00001"}],
            focus="AAA1")
        self.assertEqual(flightwall.aeroapi_usage()["queries"], 0)

    def test_the_monthly_cap_stops_spending(self):
        self._serve({"flights": [
            {"actual_off": "2026-08-17T12:00:00Z", "actual_on": None,
             "origin": {"code_icao": "KATL"}, "destination": {"code_icao": "KMSP"}},
        ]})
        flightwall.fetch_json = lambda *a, **k: {"response": "unknown callsign"}
        flightwall.AEROAPI_MONTHLY_CAP = 2
        for n in range(4):
            flightwall.route_cache._data.clear()   # force a real lookup each time
            flightwall.get_routes([{"callsign": f"DAL{n}", "lat": 39.0, "lng": -88.8}],
                                  live=f"DAL{n}")
        self.assertEqual(flightwall.aeroapi_usage()["queries"], 2,
                         "spending must stop dead at the cap, not merely slow down")

    def test_a_repeat_request_is_served_from_cache_not_bought_again(self):
        self._serve({"flights": [
            {"actual_off": "2026-08-17T12:00:00Z", "actual_on": None,
             "origin": {"code_icao": "KATL"}, "destination": {"code_icao": "KMSP"}},
        ]})
        flightwall.fetch_json = lambda *a, **k: {"response": "unknown callsign"}
        for _ in range(3):
            flightwall.get_routes([{"callsign": "DAL926", "lat": 39.0, "lng": -88.8}],
                                  live="DAL926")
        self.assertEqual(flightwall.aeroapi_usage()["queries"], 1)

    def test_coordinates_are_filled_from_the_local_airport_table(self):
        self._serve({"flights": [
            {"actual_off": "2026-08-17T12:00:00Z", "actual_on": None,
             "origin": {"code_icao": "KATL"}, "destination": {"code_icao": "KMSP"}},
        ]})
        entry = flightwall._route_via_aeroapi("DAL926")
        self.assertAlmostEqual(entry["airports"][0]["lat"], 33.6367, places=1)
        self.assertEqual(entry["airports"][-1]["iata"], "MSP")

    def test_a_landed_flight_is_not_offered_as_current(self):
        self._serve({"flights": [
            {"actual_off": "2026-08-17T09:00:00Z", "actual_on": "2026-08-17T11:00:00Z",
             "origin": {"code_icao": "KATL"}, "destination": {"code_icao": "KMSP"}},
        ]})
        self.assertIsNone(flightwall._route_via_aeroapi("DAL926"))

    def test_only_the_requested_aircraft_costs_a_query(self):
        asked = []
        real_lookup = flightwall._route_via_aeroapi

        def spy(ident):
            asked.append(ident)
            return None

        flightwall._route_via_aeroapi = spy
        flightwall.fetch_json = lambda *a, **k: {"response": "unknown callsign"}
        try:
            flightwall.get_routes([
                {"callsign": "AAA1", "lat": 38.0, "lng": -78.5, "hex": "a00001"},
                {"callsign": "BBB2", "lat": 38.1, "lng": -78.6, "hex": "a00002"},
            ], focus="BBB2", live="AAA1")
        finally:
            flightwall._route_via_aeroapi = real_lookup
        # BBB2 is the one on screen; AAA1 is the one somebody asked about.
        self.assertEqual(asked, ["AAA1"])

    def test_no_key_is_a_normal_state_not_an_error(self):
        # Asked for a live answer with no key configured: the tier is skipped
        # silently and nothing is spent, rather than surfacing an error the
        # owner cannot act on.
        flightwall.aeroapi_key = lambda: None
        flightwall.urllib.request.urlopen = lambda *a, **k: self.fail("called with no key")
        flightwall.fetch_json = lambda *a, **k: {"response": "unknown callsign"}
        out = flightwall.get_routes(
            [{"callsign": "AAA1", "lat": 38.0, "lng": -78.5}], live="AAA1")
        self.assertEqual(out["errors"], [])
        self.assertEqual(flightwall.aeroapi_usage()["queries"], 0)


class TraceReconciliationTests(unittest.TestCase):
    """The geometric check cannot tell a route from its own reverse, because
    A-to-B and B-to-A are the same line. Measured on live traffic, 62% of the
    table routes that passed that check still had the wrong origin, and the
    commonest case was an aircraft flying the return leg. Where it took off
    is an observation, so it settles the question."""

    ATL = {"iata": "ATL", "icao": "KATL", "lat": 33.6367, "lon": -84.4281}
    PHL = {"iata": "PHL", "icao": "KPHL", "lat": 39.8719, "lon": -75.2411}

    def setUp(self):
        self._real = flightwall.airports.origin_from_trace

    def tearDown(self):
        flightwall.airports.origin_from_trace = self._real

    def _entry(self):
        return {"route": "ATL-PHL", "airports": [dict(self.ATL), dict(self.PHL)]}

    def _observed(self, code):
        flightwall.airports.origin_from_trace = lambda h: (
            {"iata": code, "icao": "K" + code} if code else None
        )

    def test_matching_origin_confirms_the_record(self):
        self._observed("ATL")
        out = flightwall.reconcile_with_trace(self._entry(), "abc123")
        self.assertEqual(out["route"], "ATL-PHL")
        self.assertEqual(out["confidence"], "trace-confirmed")

    def test_departing_the_claimed_destination_kills_the_record(self):
        # An earlier build reversed these as "return legs" and manufactured
        # destinations: SWA569's record said BOS-BNA, the aircraft departed
        # BNA, the flip showed BNA-BOS - and the flight was BNA-RIC. A number
        # that flies several legs a day makes the flip a guess, and the trace
        # only ever proves the departure.
        self._observed("PHL")
        self.assertIsNone(flightwall.reconcile_with_trace(self._entry(), "abc123"))

    def test_an_unrelated_departure_kills_the_record(self):
        # Neither end matches, so the table is describing a different flight.
        self._observed("DEN")
        self.assertIsNone(flightwall.reconcile_with_trace(self._entry(), "abc123"))

    def test_no_trace_leaves_the_record_alone(self):
        # Absence of evidence is not evidence; the geometric check still stands.
        self._observed(None)
        out = flightwall.reconcile_with_trace(self._entry(), "abc123")
        self.assertEqual(out["route"], "ATL-PHL")
        self.assertNotIn("confidence", out)

    def test_descent_toward_somewhere_else_beats_a_confirmed_record(self):
        # The SWA569 class of failure with the origin RIGHT: VRS held BNA-MCO
        # for a flight running BNA-RIC. Departure confirms only the origin;
        # if the aircraft is measurably descending toward a different field,
        # the record's destination is exposed as another leg's.
        flightwall.airports.origin_from_trace = lambda h: {"iata": "ATL", "icao": "KATL"}
        real = flightwall.fetch_json
        flightwall.route_cache._data.clear()
        flightwall.fetch_json = lambda *a, **k: {"response": {"flightroute": {
            "callsign": "DAL926", "airline": {"icao": "DAL"},
            "origin": {"iata_code": "ATL", "latitude": 33.6367, "longitude": -84.4281},
            "destination": {"iata_code": "PHL", "latitude": 39.8719, "longitude": -75.2411},
        }}}
        try:
            # 2,000 ft just south of Richmond, descending on a track that
            # points at RIC - not at PHL.
            out = flightwall.get_routes([{
                "callsign": "DAL926", "hex": "aaa001",
                "lat": 37.30, "lng": -77.40, "track": 15, "gs": 180,
                "alt_baro": 2000, "baro_rate": -900,
            }], focus="DAL926")
        finally:
            flightwall.fetch_json = real
        self.assertIsNone(out["routes"]["DAL926"],
                          "a record whose destination the descent contradicts must drop")
        self.assertEqual(out["derived"]["aaa001"]["destination"]["iata"], "RIC")

    def test_only_the_focused_aircraft_is_reconciled(self):
        asked = []
        flightwall.airports.origin_from_trace = lambda h: asked.append(h)
        real = flightwall.fetch_json
        flightwall.route_cache._data.clear()
        flightwall.fetch_json = lambda *a, **k: {"response": {"flightroute": {
            "callsign": "DAL926", "airline": {"icao": "DAL"},
            "origin": {"iata_code": "ATL", "latitude": 33.6367, "longitude": -84.4281},
            "destination": {"iata_code": "PHL", "latitude": 39.8719, "longitude": -75.2411},
        }}}
        try:
            flightwall.get_routes([
                {"callsign": "DAL926", "lat": 37.0, "lng": -79.0, "hex": "aaa001"},
                {"callsign": "DAL927", "lat": 37.1, "lng": -79.1, "hex": "bbb002"},
            ], focus="DAL926")
        finally:
            flightwall.fetch_json = real
        # aaa001 twice: once reconciling the route, once deriving its origin.
        self.assertEqual(set(asked), {"aaa001"})


class AeroApiIdentTypeTests(unittest.TestCase):
    """A tail number is not a designator, and FlightAware will not resolve one
    if it is told the wrong type."""

    def setUp(self):
        self._key = flightwall.aeroapi_key
        self._open = flightwall.urllib.request.urlopen
        flightwall.aeroapi_key = lambda: "test-key"
        self.urls = []

        class Resp:
            def read(self):
                return json.dumps({"flights": []}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def spy(req, *a, **k):
            self.urls.append(req.full_url)
            return Resp()

        flightwall.urllib.request.urlopen = spy

    def tearDown(self):
        flightwall.aeroapi_key = self._key
        flightwall.urllib.request.urlopen = self._open

    def test_airline_callsign_is_a_designator(self):
        flightwall._route_via_aeroapi("UAL2402")
        self.assertIn("ident_type=designator", self.urls[0])

    def test_tail_number_is_a_registration(self):
        flightwall._route_via_aeroapi("N330JT")
        self.assertIn("ident_type=registration", self.urls[0])


class DerivedTests(unittest.TestCase):
    """What the aircraft says about itself. No route table involved, so
    nothing here can go stale."""

    def test_descending_aircraft_gets_a_destination(self):
        # 2,000 ft over Dulles, descending, pointing at it.
        out = flightwall.derive([{
            "hex": "abc123", "callsign": "UAL1181", "lat": 38.85, "lng": -77.30,
            "track": 300, "gs": 200, "alt_baro": 2000, "baro_rate": -900,
        }])
        dest = out["abc123"]["destination"]
        self.assertEqual(dest["iata"], "IAD")
        self.assertEqual(dest["confidence"], "high")
        self.assertEqual(out["abc123"]["phase"], "descent")

    def test_cruising_aircraft_gets_no_destination(self):
        # At altitude an aeroplane looks the same whether it is stopping at
        # the next field or carrying on for another two thousand miles.
        out = flightwall.derive([{
            "hex": "abc124", "lat": 38.0, "lng": -78.5, "track": 90,
            "gs": 450, "alt_baro": 37000, "baro_rate": 0,
        }])
        self.assertIsNone(out["abc124"]["destination"])
        self.assertEqual(out["abc124"]["phase"], "cruise")

    def test_a_level_step_at_altitude_is_not_an_arrival(self):
        # Selected altitude below the current one means a descent down low,
        # but in the flight levels it is routine cruise housekeeping.
        out = flightwall.derive([{
            "hex": "abc125", "lat": 38.0, "lng": -78.5, "track": 90, "gs": 450,
            "alt_baro": 37000, "baro_rate": 0, "nav_altitude_mcp": 33000,
        }])
        self.assertEqual(out["abc125"]["phase"], "cruise")
        self.assertIsNone(out["abc125"]["destination"])

    def test_origin_lookup_is_reserved_for_the_focused_aircraft(self):
        # Each origin costs a few hundred KB of track history over the wire.
        calls = []
        real = flightwall.airports.origin_from_trace
        flightwall.airports.origin_from_trace = lambda h: calls.append(h)
        try:
            flightwall.derive([
                {"hex": "aaa001", "callsign": "AAA1", "lat": 38.0, "lng": -78.5},
                {"hex": "bbb002", "callsign": "BBB2", "lat": 38.1, "lng": -78.6},
            ], focus="AAA1")
        finally:
            flightwall.airports.origin_from_trace = real
        self.assertEqual(calls, ["aaa001"])


class EmptyFeedTests(unittest.TestCase):
    """A quiet sky and a misbehaving provider look identical from one
    response. Reporting "nothing overhead" when there is something is the
    worst outcome, so both get checked against the other providers."""

    def setUp(self):
        flightwall.aircraft_cache._data.clear()
        flightwall.note_preferred(None)  # provider preference is global state
        self._real = flightwall.fetch_json

    def tearDown(self):
        flightwall.fetch_json = self._real
        flightwall.note_preferred(None)

    def test_response_without_an_aircraft_list_is_a_failure_not_zero(self):
        # A rate-limit notice or an error object is valid JSON and has no
        # aircraft in it; treating that as "sky is empty" hides real traffic.
        calls = []

        def fake(url, payload=None, timeout=8.0):
            calls.append(url)
            if "adsb.lol" in url:
                return {"message": "rate limited", "retry": 30}
            return SAMPLE

        flightwall.fetch_json = fake
        result = flightwall.get_aircraft(*ORD, 60)
        self.assertEqual(result["source"], "adsb.fi")
        self.assertTrue(result["aircraft"])
        self.assertTrue(any("no aircraft list" in e for e in result["errors"]))

    def test_a_provider_seeing_nothing_defers_to_one_that_does(self):
        def fake(url, payload=None, timeout=8.0):
            return {"ac": []} if "adsb.lol" in url else SAMPLE

        flightwall.fetch_json = fake
        result = flightwall.get_aircraft(*ORD, 60)
        self.assertEqual(result["source"], "adsb.fi")
        self.assertEqual(len(result["aircraft"]), 5)

    def test_a_genuinely_empty_sky_is_still_reported(self):
        flightwall.fetch_json = lambda *a, **k: {"ac": []}
        result = flightwall.get_aircraft(*ORD, 60)
        self.assertEqual(result["aircraft"], [])
        self.assertTrue(result["allProvidersEmpty"])

    def test_the_working_provider_is_tried_first_next_time(self):
        # A feed can stay up and fast while returning nothing for days.
        # Asking it first every time adds a wasted round trip to every refresh.
        flightwall.note_preferred(None)
        order = []

        def fake(url, payload=None, timeout=8.0):
            order.append(url)
            return {"ac": []} if "adsb.lol" in url else SAMPLE

        flightwall.fetch_json = fake
        flightwall.get_aircraft(*ORD, 60)
        self.assertEqual(len(order), 2, "adsb.lol first, then adsb.fi")

        order.clear()
        flightwall.aircraft_cache._data.clear()
        flightwall.get_aircraft(*ORD, 60)
        self.assertEqual(len(order), 1, "the feed that worked should be tried first")
        self.assertIn("adsb.fi", order[0])
        flightwall.note_preferred(None)

    def test_a_list_response_is_not_mistaken_for_aircraft(self):
        flightwall.fetch_json = lambda *a, **k: ["unexpected"]
        with self.assertRaises(RuntimeError):
            flightwall.get_aircraft(*ORD, 60)


class RoutePlausibilityTests(unittest.TestCase):
    """Route records are keyed on flight number and go stale. The aircraft is
    broadcasting where it really is, so that is the arbiter."""

    PHL = {"iata": "PHL", "lat": 39.8719, "lon": -75.2411}
    ALB = {"iata": "ALB", "lat": 42.7483, "lon": -73.8017}
    CHO = {"iata": "CHO", "lat": 38.1386, "lon": -78.4529}
    LAX = {"iata": "LAX", "lat": 33.9425, "lon": -118.4081}
    CHARLOTTESVILLE = (38.0386, -78.4529)

    def test_rejects_the_observed_bad_record(self):
        # PDT6055 was labelled PHL-ALB while descending over Charlottesville,
        # roughly 200 nm from anywhere on that route.
        entry = {"route": "PHL-ALB", "airports": [self.PHL, self.ALB]}
        self.assertFalse(flightwall.route_is_plausible(entry, *self.CHARLOTTESVILLE))

    def test_accepts_the_route_it_was_probably_flying(self):
        entry = {"route": "PHL-CHO", "airports": [self.PHL, self.CHO]}
        self.assertTrue(flightwall.route_is_plausible(entry, *self.CHARLOTTESVILLE))

    def test_accepts_an_aircraft_mid_route(self):
        entry = {"airports": [self.PHL, self.ALB]}
        self.assertTrue(flightwall.route_is_plausible(entry, 41.3, -74.5))

    def test_accepts_an_aircraft_at_either_end(self):
        entry = {"airports": [self.PHL, self.ALB]}
        self.assertTrue(flightwall.route_is_plausible(entry, self.PHL["lat"], self.PHL["lon"]))
        self.assertTrue(flightwall.route_is_plausible(entry, self.ALB["lat"], self.ALB["lon"]))

    def test_tolerates_routine_vectoring(self):
        # Being pushed 50 nm off a 200 nm leg is ordinary, not evidence of a
        # bad record.
        entry = {"airports": [self.PHL, self.ALB]}
        self.assertTrue(flightwall.route_is_plausible(entry, 41.3, -73.4))

    def test_multi_leg_is_checked_leg_by_leg(self):
        # Partway through PHL-CHO-LAX an aircraft sits far off the direct
        # PHL-to-LAX line, which endpoint-only checking would reject.
        entry = {"airports": [self.PHL, self.CHO, self.LAX]}
        self.assertTrue(flightwall.route_is_plausible(entry, *self.CHARLOTTESVILLE))

    def test_missing_coordinates_do_not_reject(self):
        # No basis for a verdict means no verdict, rather than a guess.
        entry = {"airports": [{"iata": "AAA"}, {"iata": "BBB"}]}
        self.assertTrue(flightwall.route_is_plausible(entry, *self.CHARLOTTESVILLE))
        self.assertTrue(flightwall.route_is_plausible({"airports": [self.PHL, self.ALB]}, None, None))

    def test_a_wrong_answer_makes_it_ask_the_next_source(self):
        # A provider that returns a confidently wrong route was previously
        # treated as success, so the better source was never consulted.
        real = flightwall.fetch_json
        flightwall.route_cache._data.clear()
        asked = []
        try:
            def fake(url, payload=None, timeout=8.0):
                asked.append(url)
                if "adsb.lol" in url:
                    return [{
                        "callsign": "DAL970", "_airport_codes_iata": "MSP-PDX",
                        "plausible": 1, "airline_code": "DAL",
                        "_airports": [
                            {"iata": "MSP", "lat": 44.8820, "lon": -93.2218},
                            {"iata": "PDX", "lat": 45.5887, "lon": -122.5975},
                        ],
                    }]
                return {"response": {"flightroute": {
                    "callsign": "DAL970", "airline": {"icao": "DAL"},
                    "origin": {"iata_code": "ATL", "latitude": 33.6367, "longitude": -84.4281},
                    "destination": {"iata_code": "JFK", "latitude": 40.6398, "longitude": -73.7789},
                }}}

            flightwall.fetch_json = fake
            # Over Virginia: MSP-PDX is impossible, ATL-JFK passes right over.
            out = flightwall.get_routes([{"callsign": "DAL970", "lat": 38.03, "lng": -78.48}])
        finally:
            flightwall.fetch_json = real

        entry = out["routes"]["DAL970"]
        self.assertEqual(entry["route"], "ATL-JFK", "should prefer the source that fits")
        self.assertNotIn("suspect", entry)
        self.assertTrue(any("adsbdb" in a for a in asked), "the second source must be tried")

    def test_all_sources_wrong_still_reports_the_best_available(self):
        real = flightwall.fetch_json
        flightwall.route_cache._data.clear()
        try:
            flightwall.fetch_json = lambda url, payload=None, timeout=8.0: (
                {"response": {"flightroute": {
                    "callsign": "DAL970", "airline": {"icao": "DAL"},
                    "origin": {"iata_code": "MSP", "latitude": 44.8820, "longitude": -93.2218},
                    "destination": {"iata_code": "PDX", "latitude": 45.5887, "longitude": -122.5975},
                }}} if "adsbdb" in url else {
                    "callsign": "DAL970", "_airport_codes_iata": "MSP-PDX",
                    "_airports": [
                        {"iata": "MSP", "lat": 44.8820, "lon": -93.2218},
                        {"iata": "PDX", "lat": 45.5887, "lon": -122.5975},
                    ],
                }
            )
            out = flightwall.get_routes([{"callsign": "DAL970", "lat": 38.03, "lng": -78.48}])
        finally:
            flightwall.fetch_json = real
        self.assertTrue(out["routes"]["DAL970"]["suspect"])

    def test_a_source_that_does_not_know_defers_to_one_that_does(self):
        real = flightwall.fetch_json
        flightwall.route_cache._data.clear()
        try:
            flightwall.fetch_json = lambda url, payload=None, timeout=8.0: (
                [] if "adsb.lol" in url else {"response": {"flightroute": {
                    "callsign": "DAL970", "airline": {"icao": "DAL"},
                    "origin": {"iata_code": "ATL", "latitude": 33.6367, "longitude": -84.4281},
                    "destination": {"iata_code": "JFK", "latitude": 40.6398, "longitude": -73.7789},
                }}}
            )
            out = flightwall.get_routes([{"callsign": "DAL970", "lat": 38.03, "lng": -78.48}])
        finally:
            flightwall.fetch_json = real
        self.assertEqual(out["routes"]["DAL970"]["route"], "ATL-JFK")

    def test_get_routes_flags_rather_than_drops(self):
        real = flightwall.fetch_json
        flightwall.route_cache._data.clear()
        try:
            flightwall.fetch_json = lambda *a, **k: {"response": {"flightroute": {
                "callsign": "PDT6055", "callsign_iata": "PDT6055",
                "airline": {"icao": "PDT"},
                "origin": {"iata_code": "PHL", "municipality": "Philadelphia",
                           "latitude": 39.8719, "longitude": -75.2411},
                "destination": {"iata_code": "ALB", "municipality": "Albany",
                                "latitude": 42.7483, "longitude": -73.8017},
            }}}
            out = flightwall.get_routes(
                [{"callsign": "PDT6055", "lat": 38.0386, "lng": -78.4529}]
            )
        finally:
            flightwall.fetch_json = real
        entry = out["routes"]["PDT6055"]
        self.assertTrue(entry["suspect"], "a contradicted route must be flagged")
        self.assertEqual(entry["route"], "PHL-ALB", "the rejected route stays visible for diagnosis")


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
        limit = 60 * landmarks.CLIP_MARGIN
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
