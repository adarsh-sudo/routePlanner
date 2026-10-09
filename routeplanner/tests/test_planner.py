import functools
import itertools
import math
import random

from django.test import SimpleTestCase

from routeplanner.services.geo import Route
from routeplanner.services.planner import (
    Candidate,
    NoFeasiblePlan,
    find_candidates,
    plan_fuel,
    start_reference,
)
from routeplanner.services.stations import StationIndex

from .helpers import station, straight_line

R, MPG = 500.0, 10.0


def cand(mile, price, sid=None, off=0.0):
    return Candidate(station(sid or int(mile * 1000 + price * 100), 35.0, -100.0, price), mile, off)


def greedy_cost(nodes, dest, range_miles=R, mpg=MPG):
    """Textbook gas-station greedy: optimal fuel cost for a FIXED set of stations.

    ``nodes`` is [(mile, price), ...] with nodes[0] the origin. Used as an oracle:
    the planner must match the best subset of stations under this greedy.
    Returns None if the trip is infeasible with these stations.
    """
    tank = range_miles / mpg
    n, i, fuel, cost = len(nodes), 0, 0.0, 0.0
    while True:
        mile, price = nodes[i]
        reach = mile + range_miles
        k, cheaper = i + 1, None
        while k < n and nodes[k][0] <= reach:
            if cheaper is None and nodes[k][1] < price:
                cheaper = k
            k += 1
        if cheaper is not None:
            need = (nodes[cheaper][0] - mile) / mpg
            buy = max(0.0, need - fuel)
            cost += buy * price
            fuel += buy - need
            i = cheaper
        elif dest <= reach:
            return cost + max(0.0, (dest - mile) / mpg - fuel) * price
        elif k == i + 1:
            return None
        else:
            target = min(range(i + 1, k), key=lambda t: (nodes[t][1], -nodes[t][0]))
            cost += (tank - fuel) * price
            fuel = tank - (nodes[target][0] - mile) / mpg
            i = target


def brute_force(candidates, dest, start_price, penalty):
    """Best (fuel cost + penalty x stops) over every subset of stations."""
    best = None
    for r in range(len(candidates) + 1):
        for subset in itertools.combinations(candidates, r):
            nodes = [(0, start_price)] + [(c.mile, c.station.price) for c in subset]
            fuel = greedy_cost(nodes, dest)
            if fuel is None:
                continue
            total = fuel + penalty * r
            if best is None or total < best:
                best = total
    return best


class PlanOptimalityTests(SimpleTestCase):
    def test_matches_brute_force_on_random_instances(self):
        rng = random.Random(20240607)
        checked = infeasible = 0
        for _ in range(250):
            dest = rng.randint(300, 1900)
            n = rng.randint(0, 8)
            miles = sorted(rng.sample(range(1, dest), min(n, dest - 1)))
            cands = [cand(m, round(rng.uniform(2.6, 4.6), 3), sid=i) for i, m in enumerate(miles)]
            start_price = round(rng.uniform(2.6, 4.6), 3)
            penalty = rng.choice([0, 0, 2, 10, 40])

            expected = brute_force(cands, dest, start_price, penalty)
            if expected is None:
                infeasible += 1
                with self.assertRaises(NoFeasiblePlan):
                    plan_fuel(cands, dest, start_price, R, MPG, penalty)
                continue
            plan = plan_fuel(cands, dest, start_price, R, MPG, penalty)
            got = plan.total_cost + penalty * len(plan.stops)
            self.assertAlmostEqual(got, expected, places=6, msg=f"dest={dest} pen={penalty} {miles}")
            checked += 1
        self.assertGreater(checked, 100)  # the generator must produce plenty of feasible cases
        self.assertGreater(infeasible, 0)  # ...and exercise the infeasible path too


def physical_best(cands, dest, start_price, penalty, tank):
    """Cheapest fuel cost + penalty x stops, by enumerating what a driver can physically do (mpg = 1).

    The driver fills up at the origin, drives, and at any station may leave the road (burning ``off``
    miles of range), buy any whole number of gallons that fits, and drive back (burning ``off`` again).
    It shares no maths with the planner, so it checks the detour model itself, not just the code.
    """
    nodes = sorted(cands, key=lambda c: c.mile)

    @functools.lru_cache(maxsize=None)
    def best_from(i, fuel, pos):
        best = 0.0 if dest - pos <= fuel else math.inf
        for j in range(i, len(nodes)):
            c = nodes[j]
            if c.mile - pos > fuel:
                break  # sorted by mile: nothing further is reachable either
            at_pumps = fuel - (c.mile - pos) - c.off_route
            if at_pumps < 0:
                continue  # not enough range to reach the pumps
            for buy in range(tank - int(at_pumps) + 1):
                back = at_pumps + buy - c.off_route
                if back >= 0:
                    best = min(best, buy * c.station.price + penalty + best_from(j + 1, back, c.mile))
        return best

    return min(start_price * x + best_from(0, x, 0) for x in range(tank + 1))


class DetourOptimalityTests(SimpleTestCase):
    def test_matches_physical_enumeration_with_detours(self):
        rng = random.Random(424242)
        tank, checked, infeasible, detoured = 12, 0, 0, 0
        for _ in range(300):
            dest = rng.randint(6, 40)
            n = rng.randint(0, 5)
            miles = sorted(rng.sample(range(1, dest), min(n, dest - 1)))
            cands = [
                cand(m, round(rng.uniform(2.0, 5.0), 2), sid=i, off=float(rng.choice([0, 0, 1, 2, 3])))
                for i, m in enumerate(miles)
            ]
            start_price = round(rng.uniform(2.0, 5.0), 2)
            penalty = rng.choice([0, 0.5, 2, 6])

            expected = physical_best(cands, dest, start_price, penalty, tank)
            if expected == math.inf:
                infeasible += 1
                with self.assertRaises(NoFeasiblePlan):
                    plan_fuel(cands, dest, start_price, tank, 1.0, penalty)
                continue
            plan = plan_fuel(cands, dest, start_price, tank, 1.0, penalty)
            got = plan.total_cost + penalty * len(plan.stops)
            self.assertAlmostEqual(got, expected, places=6, msg=f"dest={dest} pen={penalty} {cands}")
            detoured += any(p.detour_gallons > 0 for p in plan.stops)
            checked += 1
        self.assertGreater(checked, 100)
        self.assertGreater(infeasible, 0)
        self.assertGreater(detoured, 20)  # the generator must actually send plans off the road


class DetourTests(SimpleTestCase):
    def test_pricier_station_on_the_road_beats_a_cheaper_one_far_off(self):
        on_road, far = cand(424, 3.50, sid=1), cand(424, 3.45, sid=2, off=8.0)
        plan = plan_fuel([on_road, far], 849, 3.60, R, MPG, 10)
        self.assertEqual([p.candidate.station.id for p in plan.stops], [1])
        self.assertEqual(plan.detour_gallons, 0.0)

    def test_a_big_enough_saving_justifies_a_detour(self):
        plan = plan_fuel([cand(424, 3.50, sid=1), cand(424, 2.50, sid=2, off=3.0)], 849, 3.60, R, MPG, 10)
        self.assertEqual([p.candidate.station.id for p in plan.stops], [2])
        self.assertAlmostEqual(plan.stops[0].detour_gallons, 0.6)  # 2 x 3 mi / 10 mpg

    def test_detour_fuel_is_added_to_the_fuel_the_trip_needs(self):
        plan = plan_fuel([cand(300, 2.00, sid=1, off=5.0)], 600, 4.00, R, MPG, 0)
        self.assertEqual(len(plan.stops), 1)
        self.assertAlmostEqual(plan.stops[0].detour_gallons, 1.0)  # 10 mi round trip / 10 mpg
        self.assertAlmostEqual(plan.total_gallons, 600 / MPG + 1.0)
        self.assertAlmostEqual(plan.detour_gallons, 1.0)
        # The way out burns fuel from the starting tank (bought at 4.00), the way back fuel bought at
        # the station (2.00): every gallon is priced where it was bought.
        self.assertAlmostEqual(plan.start.gallons, 30.5)
        self.assertAlmostEqual(plan.stops[0].gallons, 30.5)
        self.assertAlmostEqual(plan.total_cost, 30.5 * 4.00 + 30.5 * 2.00)

    def test_needs_range_to_reach_the_pumps(self):
        # 100 miles of range left on arrival is plenty for a 60 mile detour; the planner still stops.
        plan = plan_fuel([cand(400, 2.00, sid=1, off=60.0)], 700, 4.00, 500.0, MPG, 0)
        self.assertEqual(len(plan.stops), 1)
        # ...but a 120 mile detour from a spot 450 miles on cannot be reached with 50 miles of range.
        with self.assertRaises(NoFeasiblePlan):
            plan_fuel([cand(450, 2.00, sid=1, off=120.0)], 900, 4.00, 500.0, MPG, 0)

    def test_invariants_with_fractional_detours(self):
        rng = random.Random(99)
        dest = 2810.7
        cands = [
            cand(m, round(rng.uniform(2.7, 4.2), 3), sid=i, off=round(rng.uniform(0, 9), 2))
            for i, m in enumerate(sorted(rng.sample(range(5, 2800), 150)))
        ]
        plan = plan_fuel(cands, dest, 3.5, R, MPG, 10)
        self.assertAlmostEqual(plan.total_gallons, dest / MPG + plan.detour_gallons, places=9)
        self.assertAlmostEqual(plan.detour_gallons, sum(p.detour_gallons for p in plan.stops), places=9)
        self.assertAlmostEqual(plan.total_cost, plan.start.cost + sum(p.cost for p in plan.stops), places=6)
        for p in plan.stops:
            self.assertLessEqual(p.gallons, R / MPG + 1e-6)  # never more than an empty-to-full tank
            self.assertAlmostEqual(p.detour_gallons, 2 * p.candidate.off_route / MPG, places=2)

    def test_zero_detour_plans_are_unchanged(self):
        cands = [cand(m, p, sid=i) for i, (m, p) in enumerate([(100, 3.6), (450, 3.1), (900, 3.3), (1300, 2.9)])]
        plan = plan_fuel(cands, 1500, 3.5, R, MPG, 10)
        self.assertEqual(plan.detour_gallons, 0.0)
        self.assertAlmostEqual(plan.total_gallons, 150.0)


class PlanInvariantTests(SimpleTestCase):
    def setUp(self):
        rng = random.Random(7)
        self.dest = 2810.7
        self.cands = [
            cand(m, round(rng.uniform(2.7, 4.2), 3), sid=i)
            for i, m in enumerate(sorted(rng.sample(range(5, 2800), 120)))
        ]

    def test_total_gallons_is_distance_over_mpg(self):
        plan = plan_fuel(self.cands, self.dest, 3.5, R, MPG, 10)
        self.assertAlmostEqual(plan.total_gallons, self.dest / MPG, places=9)

    def test_cost_is_sum_of_purchases(self):
        plan = plan_fuel(self.cands, self.dest, 3.5, R, MPG, 10)
        parts = plan.start.cost + sum(p.cost for p in plan.stops)
        self.assertAlmostEqual(plan.total_cost, parts, places=6)
        self.assertAlmostEqual(
            plan.total_gallons, plan.start.gallons + sum(p.gallons for p in plan.stops), places=9
        )

    def test_no_leg_exceeds_range(self):
        plan = plan_fuel(self.cands, self.dest, 3.5, R, MPG, 10)
        marks = [0.0] + [p.candidate.mile for p in plan.stops] + [self.dest]
        for a, b in zip(marks, marks[1:]):
            self.assertLessEqual(b - a, R + 1.0)  # +1: whole-mile rounding of markers

    def test_never_buys_more_than_a_tank_per_stop(self):
        plan = plan_fuel(self.cands, self.dest, 3.5, R, MPG, 10)
        for p in plan.stops:
            self.assertLessEqual(p.gallons, R / MPG + 1e-6)

    def test_higher_penalty_never_adds_stops_and_never_lowers_fuel_cost(self):
        results = [plan_fuel(self.cands, self.dest, 3.5, R, MPG, pen) for pen in (0, 5, 20, 100)]
        stops = [len(p.stops) for p in results]
        costs = [p.total_cost for p in results]
        self.assertEqual(stops, sorted(stops, reverse=True))
        self.assertEqual(costs, sorted(costs))

    def test_penalty_removes_micro_stops(self):
        free = plan_fuel(self.cands, self.dest, 3.5, R, MPG, 0)
        sane = plan_fuel(self.cands, self.dest, 3.5, R, MPG, 10)
        self.assertGreater(len(free.stops), len(sane.stops))
        self.assertGreaterEqual(len(sane.stops), 5)  # 2,810 mi needs >= 5 en-route stops

    def test_short_trip_with_expensive_stops_buys_only_at_origin(self):
        cands = [cand(100, 3.60), cand(200, 3.55)]
        plan = plan_fuel(cands, 300, 3.50, R, MPG, 10)
        self.assertEqual(plan.stops, [])
        self.assertAlmostEqual(plan.start.gallons, 30.0)
        self.assertAlmostEqual(plan.total_cost, 30.0 * 3.50)

    def test_short_trip_takes_a_much_cheaper_stop(self):
        plan = plan_fuel([cand(100, 2.50)], 300, 4.00, R, MPG, 10)
        self.assertEqual(len(plan.stops), 1)
        self.assertAlmostEqual(plan.start.gallons, 10.0)  # just enough to reach mile 100
        self.assertAlmostEqual(plan.stops[0].gallons, 20.0)

    def test_infeasible_gap_raises(self):
        with self.assertRaises(NoFeasiblePlan):
            plan_fuel([cand(100, 3.0), cand(700, 3.0)], 900, 3.0, R, MPG, 10)

    def test_destination_beyond_range_without_any_station_raises(self):
        with self.assertRaises(NoFeasiblePlan):
            plan_fuel([], 800, 3.0, R, MPG, 10)

    def test_no_stations_needed_within_range(self):
        plan = plan_fuel([], 450, 3.2, R, MPG, 10)
        self.assertEqual(plan.stops, [])
        self.assertAlmostEqual(plan.total_cost, 45 * 3.2)


class CandidateTests(SimpleTestCase):
    def setUp(self):
        self.route = Route(straight_line((35.0, -100.0), (35.0, -90.0), step_deg=0.01), 0.5)
        self.total = self.route.length_miles

    def _index(self, stations):
        return StationIndex(stations)

    def test_filters_by_radius_and_sorts_by_mile(self):
        off = 3 / 69.0935
        stations = [
            station(1, 35.0 + off, -92.0, 3.0),  # 3 mi off, late
            station(2, 35.0 + off, -98.0, 3.1),  # 3 mi off, early
            station(3, 35.0 + 20 / 69.0935, -95.0, 2.0),  # 20 mi off -> excluded
        ]
        out = find_candidates(self.route, self._index(stations), 10, self.total)
        self.assertEqual([c.station.id for c in out], [2, 1])
        self.assertAlmostEqual(out[0].off_route, 3, delta=0.3)
        self.assertLess(out[0].mile, out[1].mile)

    def test_same_mile_keeps_only_the_cheapest(self):
        stations = [station(1, 35.0, -95.0, 3.40), station(2, 35.0, -95.0, 3.10), station(3, 35.0, -95.0, 3.30)]
        out = find_candidates(self.route, self._index(stations), 10, self.total)
        self.assertEqual([c.station.id for c in out], [2])

    def test_same_mile_keeps_a_pricier_station_that_is_closer_to_the_road(self):
        off = 8 / 69.0935
        stations = [station(1, 35.0, -95.0, 3.50), station(2, 35.0 + off, -95.0, 3.45)]
        out = find_candidates(self.route, self._index(stations), 10, self.total)
        self.assertEqual({c.station.id for c in out}, {1, 2})  # neither beats the other on both counts

    def test_same_mile_drops_a_station_that_is_both_pricier_and_farther(self):
        off = 4 / 69.0935
        stations = [station(1, 35.0, -95.0, 3.20), station(2, 35.0 + off, -95.0, 3.40)]
        out = find_candidates(self.route, self._index(stations), 10, self.total)
        self.assertEqual([c.station.id for c in out], [1])

    def test_same_mile_keeps_the_cheaper_farther_one_and_drops_a_dominated_third(self):
        off = lambda mi: mi / 69.0935
        stations = [
            station(1, 35.0, -95.0, 3.50),  # on the road
            station(2, 35.0 + off(8), -95.0, 3.40),  # cheaper, far
            station(3, 35.0 + off(9), -95.0, 3.45),  # pricier than 2 and farther: dominated
        ]
        out = find_candidates(self.route, self._index(stations), 10, self.total)
        self.assertEqual({c.station.id for c in out}, {1, 2})

    def test_miles_rescaled_to_reported_distance(self):
        out = find_candidates(self.route, self._index([station(1, 35.0, -90.0, 3.0)]), 10, self.total * 1.01)
        self.assertAlmostEqual(out[0].mile, self.total * 1.01, delta=1.0)


class StartReferenceTests(SimpleTestCase):
    def test_cheapest_within_radius_else_nearest(self):
        from routeplanner.services.geo import haversine_miles as hav

        idx = StationIndex(
            [
                station(1, 35.0, -100.0, 3.9),  # at origin
                station(2, 35.2, -100.0, 3.2),  # ~14 mi, cheaper
                station(3, 36.5, -100.0, 2.8),  # ~100 mi, cheapest but outside 50
            ]
        )
        s, d = start_reference(idx, 35.0, -100.0, 50, hav)
        self.assertEqual(s.id, 2)
        s, d = start_reference(idx, 38.0, -100.0, 50, hav)  # nothing within 50 -> nearest
        self.assertEqual(s.id, 3)
