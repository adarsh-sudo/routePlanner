"""OpenRouteService (truck) routing: the requests it builds, how its answers and errors are read, and the whole
trip through the API with the service mocked (no network, no key needed)."""

import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import requests
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from routeplanner.services import ors, routing, stations
from routeplanner.services.geo import Route
from routeplanner.services.planner import Candidate

from .helpers import FakeResponse, fake_ors_matrix, fake_ors_route, station, straight_line
from .test_api import FINISH, START, make_stations

KEY = "test-key-not-a-real-one"
M = 1609.344  # metres per mile
ORS = dict(ROUTING_ENGINE="ors", ORS_API_KEY=KEY, ORS_MIN_INTERVAL_SECONDS=0)
TRUCK = {"HEIGHT_M": 4.11, "WIDTH_M": 2.59, "LENGTH_M": 22.0, "WEIGHT_T": 36.3, "AXLE_LOAD_T": None, "HAZMAT": False}


class RequestAndAnswerTests(SimpleTestCase):
    def test_restrictions_are_the_truck_in_metres_and_tonnes(self):
        self.assertEqual(
            ors.restrictions(TRUCK),
            {"height": 4.11, "width": 2.59, "length": 22.0, "weight": 36.3, "hazmat": False},
        )

    def test_an_axle_limit_and_hazmat_are_sent_only_when_set(self):
        out = ors.restrictions({**TRUCK, "AXLE_LOAD_T": 9.0, "HAZMAT": True})
        self.assertEqual((out["axleload"], out["hazmat"]), (9.0, True))
        self.assertNotIn("axleload", ors.restrictions(TRUCK))

    def test_route_body_sends_longitude_first_and_asks_for_a_truck(self):
        body = ors.route_body((25.7617, -80.1918), (47.6062, -122.3321), TRUCK)
        # [lon, lat]: the order the bare "miami" bug was wrongly blamed on
        self.assertEqual(body["coordinates"], [[-80.1918, 25.7617], [-122.3321, 47.6062]])
        self.assertEqual(body["options"]["vehicle_type"], "hgv")
        self.assertEqual(body["options"]["profile_params"]["restrictions"]["height"], 4.11)
        self.assertEqual(body["radiuses"], [-1, -1])  # snap however far: a city centre can be off the road network
        self.assertFalse(body["instructions"])

    def test_the_route_answer_is_read_back_as_lat_lon_points(self):
        points = straight_line((35.0, -100.0), (35.5, -99.0))
        got, meters, seconds = ors.parse_route(fake_ors_route(points))
        self.assertEqual(len(got), len(points))
        for (lat, lon), (want_lat, want_lon) in zip(got, points):
            self.assertAlmostEqual(lat, want_lat, places=4)  # 5-digit polyline
            self.assertAlmostEqual(lon, want_lon, places=4)
        self.assertGreater(meters, 80_000)
        self.assertGreater(seconds, 0)

    def test_an_answer_with_no_route_is_rejected(self):
        for data in ({}, {"routes": []}, {"routes": [{"summary": {}}]}, None):
            with self.assertRaises(ValueError):
                ors.parse_route(data)

    def test_matrix_body_lists_a_then_s_then_b_and_asks_for_distance_in_metres(self):
        a, s, b = [(35.0, -99.0), (35.0, -98.0)], [(35.1, -99.0), (35.1, -98.0)], [(35.0, -97.0), (35.0, -96.0)]
        body = ors.matrix_body(a + s + b, 2)
        self.assertEqual(body["locations"][2], [-99.0, 35.1])  # [lon, lat], the S block after the A block
        self.assertEqual(body["sources"], ["0", "1", "2", "3"])
        self.assertEqual(body["destinations"], ["2", "3", "4", "5"])
        self.assertEqual((body["metrics"], body["units"]), (["distance"], "m"))

    def test_matrix_answer_must_be_the_size_asked_for(self):
        rows = [[1.0, None], [2.0, 3.0]]
        self.assertEqual(ors.parse_matrix({"distances": rows}, 1), rows)  # None (no route) is kept
        for data in ({}, {"distances": rows}, {"distances": [[1.0, 2.0], [3.0]]}, {"distances": None}):
            with self.assertRaises(ValueError):
                ors.parse_matrix(data, 2)

    def test_errors_are_sorted_into_no_route_too_long_or_other(self):
        err = lambda code, msg="": {"error": {"code": code, "message": msg}}  # noqa: E731
        self.assertEqual(ors.explain(404, err(2010, "Could not find routable point"))[0], "no_route")
        self.assertEqual(ors.explain(404, err(2009, "Route could not be found"))[0], "no_route")
        kind, message = ors.explain(400, err(2004, "The approximated route distance must not be greater than 6000000.0 meters."))
        self.assertEqual(kind, "too_long")
        self.assertIn("6,000 km", message)
        # code 2004 is a general "over a limit" code: a different limit is not a "too long" trip
        self.assertEqual(ors.explain(400, err(2004, "Too many waypoints"))[0], "error")
        self.assertIn("ORS_API_KEY", ors.explain(403, {"error": "Access to this API has been disallowed"})[1])
        self.assertIn("rate limit", ors.explain(429, {})[1])
        self.assertIn("6010", ors.explain(400, err(6010, "Point not found"))[1])
        self.assertEqual(ors.explain(502, None)[0], "error")  # an HTML error page has no JSON body


@override_settings(**ORS)
class RoutingTests(SimpleTestCase):
    def setUp(self):
        routing.clear_cache()
        self.points = straight_line(START, FINISH)
        self.post = self.patch(routing._session, "post", return_value=FakeResponse(fake_ors_route(self.points)))
        self.get = self.patch(routing._session, "get", return_value=FakeResponse({}))

    def patch(self, obj, name, **kw):
        patcher = mock.patch.object(obj, name, **kw)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def answer_with_error(self, status, body):
        self.post.return_value = FakeResponse(body, status=status)

    # -- the route ---------------------------------------------------------
    def test_a_route_is_one_post_with_the_key_in_a_header_and_nowhere_else(self):
        result, calls = routing.get_route(START, FINISH, 0.5)
        self.assertEqual(calls, 1)
        (url,), kwargs = self.post.call_args
        self.assertEqual(url, "https://api.openrouteservice.org/v2/directions/driving-hgv")
        self.assertEqual(kwargs["headers"]["Authorization"], KEY)
        self.assertNotIn(KEY, url)
        self.assertNotIn(KEY, repr(kwargs["json"]))
        self.assertEqual(kwargs["json"]["coordinates"][0], [START[1], START[0]])
        self.assertGreater(result.distance_miles, 800)
        self.assertGreater(result.duration_hours, 10)
        self.assertEqual(self.get.call_count, 0)

    def test_a_repeated_trip_is_served_from_the_cache(self):
        routing.get_route(START, FINISH, 0.5)
        _, calls = routing.get_route(START, FINISH, 0.5)
        self.assertEqual((calls, self.post.call_count), (0, 1))

    def test_the_cache_does_not_mix_engines(self):
        routing.get_route(START, FINISH, 0.5)
        osrm = {"code": "Ok", "routes": [{"geometry": "", "distance": 1, "duration": 1}]}
        with override_settings(ROUTING_ENGINE="osrm", OSRM_MIN_INTERVAL_SECONDS=0), \
                mock.patch.object(routing, "decode_polyline", return_value=self.points):
            self.get.return_value = FakeResponse(osrm)
            _, calls = routing.get_route(START, FINISH, 0.5)
        self.assertEqual(calls, 1)  # the truck route above was not reused for a car

    def test_places_with_no_road_between_them(self):
        self.answer_with_error(404, {"error": {"code": 2009, "message": "Route could not be found"}})
        with self.assertRaises(routing.NoRouteFound):
            routing.get_route(START, FINISH, 0.5)

    def test_a_trip_over_the_distance_limit(self):
        self.answer_with_error(400, {"error": {"code": 2004, "message": "The approximated route distance must not be greater than 6000000.0 meters."}})
        with self.assertRaises(routing.RouteTooLong) as ctx:
            routing.get_route(START, FINISH, 0.5)
        self.assertIsInstance(ctx.exception, routing.RoutingError)  # so a caller that only knows RoutingError still copes

    def test_a_refused_key_and_a_rate_limit_are_routing_errors_that_say_so(self):
        for status, words in ((401, "ORS_API_KEY"), (403, "ORS_API_KEY"), (429, "rate limit")):
            self.answer_with_error(status, {"error": "no"})
            with self.assertRaisesRegex(routing.RoutingError, words) as ctx:
                routing.get_route(START, FINISH, 0.5)
            self.assertNotIsInstance(ctx.exception, routing.NoRouteFound)
            self.assertNotIn(KEY, str(ctx.exception))

    def test_a_network_failure_and_a_page_that_is_not_json(self):
        self.post.side_effect = requests.ConnectionError("down")
        with self.assertRaisesRegex(routing.RoutingError, "unavailable"):
            routing.get_route(START, FINISH, 0.5)
        self.post.side_effect = None
        self.post.return_value = mock.Mock(status_code=502, json=mock.Mock(side_effect=ValueError("<html>")))
        with self.assertRaisesRegex(routing.RoutingError, "unavailable"):
            routing.get_route(START, FINISH, 0.5)

    def test_a_200_answer_without_a_route_is_an_error_not_a_crash(self):
        self.post.return_value = FakeResponse({"routes": []})
        with self.assertRaisesRegex(routing.RoutingError, "no route"):
            routing.get_route(START, FINISH, 0.5)

    # -- the detour matrix ---------------------------------------------------
    def make_candidates(self):
        self.route = Route(straight_line((35.0, -100.0), (35.0, -90.0), step_deg=0.01), 0.5)
        self.c1 = Candidate(station(1, 35.1, -97.0, 3.0), 200.0, 6.9, self.route.index_at(200.0))
        self.c2 = Candidate(station(2, 35.1, -94.0, 3.1), 400.0, 6.9, self.route.index_at(400.0))

    # Rows A1 A2 S1 S2, columns S1 S2 B1 B2, in miles (9999 = a number nobody should read)
    MATRIX = [[15, 9999, 20, 9999], [9999, 14, 9999, 20], [0, 9999, 16, 9999], [9999, 0, 9999, 18]]

    def answer_matrix(self, rows):
        self.post.return_value = FakeResponse({"distances": [[None if x is None else x * M for x in r] for r in rows]})

    def test_detours_are_one_matrix_post_and_the_extra_miles_are_there_and_back_minus_direct(self):
        self.make_candidates()
        self.answer_matrix(self.MATRIX)
        found, calls = routing.driven_detours(self.route, [self.c1, self.c2], 10)
        self.assertEqual(calls, 1)
        self.assertAlmostEqual(found[1], 15 + 16 - 20)
        self.assertAlmostEqual(found[2], 14 + 18 - 20)
        (url,), kwargs = self.post.call_args
        self.assertEqual(url, "https://api.openrouteservice.org/v2/matrix/driving-hgv")
        self.assertEqual(kwargs["headers"]["Authorization"], KEY)
        self.assertEqual(len(kwargs["json"]["locations"]), 6)  # A1 A2 S1 S2 B1 B2
        self.assertEqual(kwargs["json"]["sources"], ["0", "1", "2", "3"])
        self.assertEqual(kwargs["json"]["destinations"], ["2", "3", "4", "5"])

    def test_a_station_with_no_route_is_left_out_and_results_are_cached(self):
        self.make_candidates()
        self.answer_matrix([[None if (r, c) == (0, 0) else x for c, x in enumerate(row)] for r, row in enumerate(self.MATRIX)])
        found, _ = routing.driven_detours(self.route, [self.c1, self.c2], 10)
        self.assertNotIn(1, found)
        self.assertIn(2, found)
        _, calls = routing.driven_detours(self.route, [self.c2], 10)
        self.assertEqual(calls, 0)  # measured once, remembered

    def test_a_detour_measured_for_a_truck_is_not_reused_for_a_car(self):
        self.make_candidates()
        self.answer_matrix([[15, 20], [0, 16]])  # one station: A->S 15, A->B 20, S->B 16
        found, _ = routing.driven_detours(self.route, [self.c1], 10)
        self.assertAlmostEqual(found[1], 11)
        with override_settings(ROUTING_ENGINE="osrm", OSRM_MIN_INTERVAL_SECONDS=0):
            self.get.return_value = FakeResponse({"code": "Ok", "distances": [[10 * M, 20 * M], [0, 12 * M]]})
            found, calls = routing.driven_detours(self.route, [self.c1], 10)
        self.assertEqual(calls, 1)  # measured again, on the car engine
        self.assertAlmostEqual(found[1], 2)

    def test_no_more_stations_are_sent_than_the_free_plan_takes(self):
        self.make_candidates()
        many = [Candidate(station(i, 35.1, -99.0 + i * 0.1, 3.0), 100.0 + i, 6.9, self.route.index_at(100.0 + i)) for i in range(40)]
        self.post.side_effect = lambda url, **kw: FakeResponse(fake_ors_matrix(kw["json"]))
        found, calls = routing.driven_detours(self.route, many, 10)
        self.assertEqual((calls, len(found)), (1, ors.MAX_MATRIX_STATIONS))
        self.assertEqual(len(self.post.call_args.kwargs["json"]["locations"]), 3 * ors.MAX_MATRIX_STATIONS)

    def test_a_failed_matrix_call_raises_routing_error(self):
        self.make_candidates()
        self.answer_with_error(429, {})
        with self.assertRaises(routing.RoutingError):
            routing.driven_detours(self.route, [self.c1], 10)
        self.post.return_value = FakeResponse({"distances": [[1.0]]})  # wrong size
        with self.assertRaisesRegex(routing.RoutingError, "size"):
            routing.driven_detours(self.route, [self.c1], 10)

    # -- warm-up ---------------------------------------------------------------
    def test_warming_the_connection_never_sends_the_key(self):
        with mock.patch.object(routing, "_last_call_at", 0.0):
            self.assertTrue(routing.warm())
        (url,), kwargs = self.get.call_args
        self.assertEqual(url, "https://api.openrouteservice.org/")
        self.assertNotIn("Authorization", kwargs["headers"])  # so it cannot use up any of the daily quota
        self.assertEqual(self.post.call_count, 0)

    def test_the_response_describes_a_truck(self):
        info = routing.describe()
        self.assertEqual((info["engine"], info["vehicle"]), ("openrouteservice", "truck"))
        self.assertEqual(info["credit"], "openrouteservice.org by HeiGIT")
        self.assertEqual(info["truck"]["height_m"], settings.TRUCK["HEIGHT_M"])
        self.assertIsNone(info["note"])  # a truck route needs no warning
        with override_settings(ROUTING_ENGINE="osrm"):
            car = routing.describe()
        self.assertEqual((car["engine"], car["vehicle"], car["credit"], car["truck"]), ("osrm", "car", "OSRM", None))
        self.assertIn("not a truck route", car["note"])  # the page shows this, so a car route is never passed off as a truck's
        self.assertIn("ORS_API_KEY", car["note"])


@override_settings(**ORS)
class TripThroughTheApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_stations()

    def setUp(self):
        stations.get_station_index.cache_clear()
        routing.clear_cache()
        self.route_answer = FakeResponse(fake_ors_route(straight_line(START, FINISH)))
        self.matrix_error = None
        patcher = mock.patch.object(routing._session, "post", side_effect=self._ors)
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def _ors(self, url, **kwargs):
        if "/matrix/" in url:
            return self.matrix_error or FakeResponse(fake_ors_matrix(kwargs["json"]))
        return self.route_answer

    def calls(self, service):
        return [c for c in self.post.call_args_list if f"/{service}/" in c.args[0]]

    def get(self, path="/api/route/"):
        return self.client.get(path, {"start": "35.0,-100.0", "finish": "35.0,-85.0"})

    def test_a_trip_is_planned_on_a_truck_route_with_one_route_call_and_one_matrix_call(self):
        resp = self.get()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["routing"]["vehicle"], "truck")
        self.assertEqual(body["routing"]["truck"]["weight_t"], settings.TRUCK["WEIGHT_T"])
        self.assertEqual((len(self.calls("directions")), len(self.calls("matrix"))), (1, 1))
        self.assertEqual(body["api_calls"]["routing"], 2)
        self.assertTrue(body["summary"]["driven_detours"])
        self.assertGreater(body["summary"]["num_fuel_stops"], 0)
        self.assertNotIn(KEY, resp.content.decode())  # the key never reaches a response

    def test_the_map_page_credits_openrouteservice_and_says_the_route_is_for_a_truck(self):
        html = self.get("/api/route/map/").content.decode()
        self.assertIn("Routes by openrouteservice.org by HeiGIT.", html)
        self.assertIn('"vehicle": "truck"', html)
        self.assertNotIn(KEY, html)

    def test_a_trip_over_the_distance_limit_is_a_422_with_its_own_hint(self):
        self.route_answer = FakeResponse(
            {"error": {"code": 2004, "message": "The approximated route distance must not be greater than 6000000.0 meters."}},
            status=400,
        )
        resp = self.get()
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"]["code"], "route_too_long")
        page = self.get("/api/route/map/")
        self.assertContains(page, "Split the trip in two", status_code=422)

    def test_a_refused_key_is_a_502_that_names_the_key(self):
        self.route_answer = FakeResponse({"error": "Access to this API has been disallowed"}, status=403)
        resp = self.get()
        self.assertEqual(resp.status_code, 502)
        self.assertIn("ORS_API_KEY", resp.json()["error"]["message"])

    def test_if_only_the_matrix_fails_the_plan_still_comes_back_with_straight_line_detours(self):
        self.matrix_error = FakeResponse({"error": {"code": 6010, "message": "Point not found"}}, status=400)
        resp = self.get()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["summary"]["driven_detours"])


class ConfigurationTests(SimpleTestCase):
    def import_settings(self, **env):
        # DOTENV_PATH points at a file that does not exist, so a real .env in the project cannot change the result
        nothing = str(Path(tempfile.gettempdir()) / "fuelroute-test-no-such.env")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("ROUTING_", "ORS_"))}
        return subprocess.run(
            [sys.executable, "-c", "import config.settings"], cwd=settings.BASE_DIR,
            env={**base, "DOTENV_PATH": nothing, **env}, capture_output=True, text=True, timeout=60,
        )

    def engine_with(self, **env):
        nothing = str(Path(tempfile.gettempdir()) / "fuelroute-test-no-such.env")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("ROUTING_", "ORS_"))}
        done = subprocess.run(
            [sys.executable, "-c", "import config.settings as s; print(s.ROUTING_ENGINE)"], cwd=settings.BASE_DIR,
            env={**base, "DOTENV_PATH": nothing, **env}, capture_output=True, text=True, timeout=60,
        )
        return done.stdout.strip() if done.returncode == 0 else f"FAILED: {done.stderr[-200:]}"

    def test_the_default_needs_no_key(self):
        self.assertEqual(self.import_settings().returncode, 0)

    def test_a_key_alone_switches_on_truck_routing(self):
        self.assertEqual(self.engine_with(ORS_API_KEY=KEY), "ors")  # no ROUTING_ENGINE line needed

    def test_with_no_key_the_app_still_runs_on_car_routes(self):
        self.assertEqual(self.engine_with(), "osrm")
        self.assertEqual(self.engine_with(ORS_API_KEY="  "), "osrm")  # a blank key is no key

    def test_car_routes_can_still_be_forced_even_with_a_key(self):
        self.assertEqual(self.engine_with(ORS_API_KEY=KEY, ROUTING_ENGINE="osrm"), "osrm")
        self.assertEqual(self.engine_with(ORS_API_KEY=KEY, ROUTING_ENGINE=""), "ors")  # blank counts as not set

    def test_truck_routing_without_a_key_stops_the_server_with_a_clear_message(self):
        done = self.import_settings(ROUTING_ENGINE="ors")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ORS_API_KEY", done.stderr)
        self.assertEqual(self.import_settings(ROUTING_ENGINE="ors", ORS_API_KEY="  ").returncode, 1)  # blank is no key

    def test_truck_routing_with_a_key_and_an_unknown_engine(self):
        self.assertEqual(self.import_settings(ROUTING_ENGINE="ORS", ORS_API_KEY=KEY).returncode, 0)  # case does not matter
        done = self.import_settings(ROUTING_ENGINE="google")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("'osrm' or 'ors'", done.stderr)


@override_settings(**ORS)
class CheckCommandTests(SimpleTestCase):
    def setUp(self):
        routing.clear_cache()
        self.addCleanup(routing.clear_cache)

    def run_command(self, post):
        out = StringIO()
        with mock.patch.object(routing._session, "post", side_effect=post):
            call_command("check_ors", stdout=out)
        return out.getvalue()

    @staticmethod
    def healthy(url, **kwargs):
        if "/matrix/" in url:
            return FakeResponse(fake_ors_matrix(kwargs["json"]))
        return FakeResponse(fake_ors_route(straight_line((32.7767, -96.7970), (32.7555, -97.3308))))

    def test_it_reports_a_working_key(self):
        text = self.run_command(self.healthy)
        self.assertIn("Route OK", text)
        self.assertIn("Detour OK", text)
        self.assertNotIn(KEY, text)

    def test_it_says_when_there_is_no_key(self):
        with override_settings(ORS_API_KEY=""), self.assertRaisesRegex(CommandError, "ORS_API_KEY is not set"):
            call_command("check_ors", stdout=StringIO())

    def test_it_reports_a_refused_key_as_a_failure(self):
        refused = lambda url, **kw: FakeResponse({"error": "Access to this API has been disallowed"}, status=403)  # noqa: E731
        with self.assertRaisesRegex(CommandError, "Route request failed.*ORS_API_KEY"):
            self.run_command(refused)

    def test_it_reports_a_failed_matrix_separately(self):
        def route_only(url, **kwargs):
            if "/matrix/" in url:
                return FakeResponse({"error": {"code": 6010, "message": "Point not found"}}, status=400)
            return self.healthy(url, **kwargs)

        with self.assertRaisesRegex(CommandError, "Detour .* failed"):
            self.run_command(route_only)
