"""Real driven detours: the OSRM table call, its cache, and how the planner uses the result."""

import dataclasses
from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from routeplanner.services import routing
from routeplanner.services.geo import Route, haversine_miles
from routeplanner.services.planner import Candidate, Plan, detour_checks, plan_fuel
from routeplanner.services.routing import RoutingError, driven_detours

from .helpers import FakeResponse, station, straight_line

M = 1609.344  # meters per mile


def miles(rows):
    return [[None if x is None else x * M for x in row] for row in rows]


class RouteIndexTests(SimpleTestCase):
    def test_index_at_is_clamped_to_the_ends(self):
        route = Route(straight_line((35.0, -100.0), (35.0, -90.0), step_deg=0.01), 0.5)
        self.assertEqual(route.index_at(-5), 0)
        self.assertEqual(route.index_at(10**6), len(route.points) - 1)
        i = route.index_at(100.0)
        self.assertLessEqual(route.miles[i], 100.0)
        self.assertGreater(route.miles[i] + 1.0, 100.0)


@override_settings(ROUTING_ENGINE="osrm", OSRM_MIN_INTERVAL_SECONDS=0)
class DrivenDetourTests(SimpleTestCase):
    def setUp(self):
        routing.clear_cache()
        self.route = Route(straight_line((35.0, -100.0), (35.0, -90.0), step_deg=0.01), 0.5)
        self.c1 = Candidate(station(1, 35.1, -97.0, 3.0), 200.0, 6.9, self.route.index_at(200.0))
        self.c2 = Candidate(station(2, 35.1, -94.0, 3.1), 400.0, 6.9, self.route.index_at(400.0))

    def respond(self, rows, status=200, code="Ok"):
        body = {"code": code, "distances": miles(rows)} if rows is not None else {"code": code}
        patcher = mock.patch.object(routing._session, "get", return_value=FakeResponse(body, status=status))
        self.get = patcher.start()
        self.addCleanup(patcher.stop)

    # Rows: A1 A2 S1 S2.  Columns: S1 S2 B1 B2.   (9999 = a number nobody should read)
    MATRIX = [
        [15, 9999, 20, 9999],   # A1 -> S1 = 15, A1 -> B1 = 20
        [9999, 14, 9999, 20],   # A2 -> S2 = 14, A2 -> B2 = 20
        [0, 9999, 16, 9999],    # S1 -> B1 = 16
        [9999, 0, 9999, 18],    # S2 -> B2 = 18
    ]

    def test_extra_miles_are_there_and_back_minus_the_direct_drive(self):
        self.respond(self.MATRIX)
        found, calls = driven_detours(self.route, [self.c1, self.c2], 10)
        self.assertEqual(calls, 1)
        self.assertAlmostEqual(found[1], 15 + 16 - 20)  # 11
        self.assertAlmostEqual(found[2], 14 + 18 - 20)  # 12

    def test_request_is_one_table_call_with_a_s_b_coordinates(self):
        self.respond(self.MATRIX)
        driven_detours(self.route, [self.c1, self.c2], 10)
        self.assertEqual(self.get.call_count, 1)
        url, kwargs = self.get.call_args.args[0], self.get.call_args.kwargs
        self.assertIn("/table/v1/driving/", url)
        self.assertEqual(len(url.split("/driving/")[1].split(";")), 6)  # A1 A2 S1 S2 B1 B2
        self.assertEqual(kwargs["params"]["annotations"], "distance")
        self.assertEqual(kwargs["params"]["sources"], "0;1;2;3")
        self.assertEqual(kwargs["params"]["destinations"], "2;3;4;5")
        coords = [tuple(map(float, c.split(","))) for c in url.split("/driving/")[1].split(";")]  # (lon, lat)
        self.assertAlmostEqual(coords[2][1], 35.1, places=3)  # the S's sit in the middle block
        anchor = self.route.points[self.c1.route_idx]
        a1, b1 = (coords[0][1], coords[0][0]), (coords[4][1], coords[4][0])
        self.assertAlmostEqual(haversine_miles(*anchor, *a1), 10.0, delta=0.6)  # A1: ~10 mi before the anchor
        self.assertAlmostEqual(haversine_miles(*anchor, *b1), 10.0, delta=0.6)  # B1: ~10 mi after it
        self.assertLess(a1[1], anchor[1])
        self.assertGreater(b1[1], anchor[1])

    def test_a_station_the_service_cannot_route_to_is_left_out(self):
        rows = [r[:] for r in self.MATRIX]
        rows[3][3] = None
        self.respond(rows)
        found, _ = driven_detours(self.route, [self.c1, self.c2], 10)
        self.assertIn(1, found)
        self.assertNotIn(2, found)

    def test_negative_or_noisy_values_never_give_a_negative_detour(self):
        self.respond([[5, 9999, 20, 9999], [9999, 1, 9999, 20], [0, 9999, 5, 9999], [9999, 0, 9999, 1]])
        found, _ = driven_detours(self.route, [self.c1, self.c2], 10)
        self.assertEqual(found, {1: 0.0, 2: 0.0})

    def test_repeat_is_served_from_the_cache_without_a_call(self):
        self.respond(self.MATRIX)
        driven_detours(self.route, [self.c1, self.c2], 10)
        found, calls = driven_detours(self.route, [self.c1, self.c2], 10)
        self.assertEqual(calls, 0)
        self.assertEqual(self.get.call_count, 1)
        self.assertAlmostEqual(found[1], 11)

    def test_only_stations_missing_from_the_cache_are_requested(self):
        self.respond(self.MATRIX)
        driven_detours(self.route, [self.c1, self.c2], 10)
        c3 = Candidate(station(3, 35.1, -92.0, 3.2), 600.0, 6.9, self.route.index_at(600.0))
        self.respond([[12, 20], [0, 13]])  # rows A3, S3; columns S3, B3
        found, calls = driven_detours(self.route, [self.c1, c3], 10)
        self.assertEqual(calls, 1)
        url = self.get.call_args.args[0]
        self.assertEqual(len(url.split("/driving/")[1].split(";")), 3)  # just c3's A, S, B
        self.assertEqual(set(found), {1, 3})
        self.assertAlmostEqual(found[1], 11)  # c1 came from the cache
        self.assertAlmostEqual(found[3], 12 + 13 - 20)

    def test_at_most_a_third_of_the_table_limit_is_asked_for(self):
        cands = [
            Candidate(station(i, 35.1, -99.0 + i * 0.01, 3.0), 10.0 + i, 6.9, self.route.index_at(10.0 + i))
            for i in range(50)
        ]
        n = routing.MAX_TABLE_COORDS // 3  # 33
        self.respond([[0] * (2 * n) for _ in range(2 * n)])
        found, calls = driven_detours(self.route, cands, 10)
        self.assertEqual(calls, 1)
        self.assertLessEqual(len(self.get.call_args.args[0].split("/driving/")[1].split(";")), routing.MAX_TABLE_COORDS)
        self.assertEqual(len(found), n)

    def test_service_errors_raise_routing_error(self):
        self.respond(None, status=400, code="TooBig")
        with self.assertRaises(RoutingError):
            driven_detours(self.route, [self.c1], 10)

    def test_network_failure_raises_routing_error(self):
        with mock.patch.object(routing._session, "get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(RoutingError):
                driven_detours(self.route, [self.c1], 10)


class CandidateDetourTests(SimpleTestCase):
    def test_planner_charges_half_the_driven_round_trip_each_way(self):
        c = Candidate(station(1, 35.0, -100.0, 2.0), 300.0, 5.0)
        self.assertEqual(c.off, 5.0)  # straight line until measured
        measured = dataclasses.replace(c, detour=14.0)
        self.assertEqual(measured.off, 7.0)  # half of a 14 mile round trip
        self.assertEqual(measured.off_route, 5.0)  # the straight line is still reported

    def test_plan_uses_the_driven_detour_for_fuel(self):
        c = Candidate(station(1, 35.0, -100.0, 2.0), 300.0, 0.0, detour=20.0)
        plan = plan_fuel([c], 600, 4.00, 500.0, 10.0, 0)
        self.assertAlmostEqual(plan.detour_gallons, 2.0)  # 20 mi / 10 mpg
        self.assertAlmostEqual(plan.total_gallons, 62.0)

    def test_a_long_driven_detour_makes_a_cheaper_station_lose(self):
        near_and_dear = Candidate(station(1, 35.0, -100.0, 3.50), 424.0, 0.0)
        cheap = Candidate(station(2, 35.0, -100.0, 3.40), 424.0, 1.0)  # looks 1 mile off...
        straight = plan_fuel([near_and_dear, cheap], 849, 3.60, 500.0, 10.0, 10)
        driven = plan_fuel([near_and_dear, dataclasses.replace(cheap, detour=40.0)], 849, 3.60, 500.0, 10.0, 10)
        self.assertEqual([p.candidate.station.id for p in straight.stops], [2])
        self.assertEqual([p.candidate.station.id for p in driven.stops], [1])  # ...but is really 40 mi round trip


class DetourChecksTests(SimpleTestCase):
    def cands(self, miles_prices, location="site"):
        return [
            Candidate(station(i, 35.0, -100.0, p, location=location), float(m), 0.0)
            for i, (m, p) in enumerate(miles_prices, 1)
        ]

    def test_only_stations_matched_to_a_real_fuel_station_are_measured(self):
        def cand(sid, mile, location):
            return Candidate(station(sid, 35.0, -100.0, 3.0, location=location), float(mile), 0.0)

        site_a, site_b = cand(1, 100, "site"), cand(2, 110, "site")
        at_exit, in_city = cand(3, 105, "exit"), cand(4, 108, "city")
        everyone = [site_a, site_b, at_exit, in_city]
        out = detour_checks(self.plan_with([site_a, at_exit]), everyone, limit=10)
        self.assertEqual({c.station.id for c in out}, {1, 2})  # not the exit-level or city-level ones
        # a plan whose stops are all guessed positions needs no measurement at all
        self.assertEqual(detour_checks(self.plan_with([at_exit, in_city]), everyone, limit=10), [])

    def plan_with(self, chosen):
        return Plan(None, [type("P", (), {"candidate": c})() for c in chosen], 0.0, 0.0)

    def test_chosen_stops_come_first_then_the_nearest_stand_ins(self):
        cands = self.cands([(100, 3.0), (110, 3.2), (300, 3.1), (305, 3.5), (900, 3.0)])
        out = detour_checks(self.plan_with([cands[0], cands[2]]), cands, limit=4)
        ids = [c.station.id for c in out]
        self.assertEqual(ids[:2], [1, 3])  # the chosen ones
        self.assertEqual(set(ids[2:]), {2, 4})  # 110 and 305 are nearest to a chosen stop; 900 is not
        self.assertEqual(len(ids), 4)

    def test_limit_is_respected_and_a_plan_without_stops_needs_no_check(self):
        cands = self.cands([(m, 3.0) for m in range(100, 200, 5)])
        self.assertEqual(len(detour_checks(self.plan_with([cands[0]]), cands, limit=5)), 5)
        self.assertEqual(detour_checks(self.plan_with([]), cands, limit=5), [])
