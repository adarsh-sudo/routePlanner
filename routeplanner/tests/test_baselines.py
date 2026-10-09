from django.test import SimpleTestCase

from routeplanner.services.baselines import refuel_rule
from routeplanner.services.planner import Candidate, plan_fuel

from .helpers import station

R, MPG = 500.0, 10.0
USED, LEFT = 0.75 * R, 0.25 * R  # trigger levels: "25% used" and "25% left"


def cand(mile, price, off=0.0, sid=None):
    return Candidate(station(sid or int(mile * 10 + price * 100), 35.0, -100.0, price), float(mile), off)


def every(step, last, price=3.0):
    return [cand(m, price) for m in range(step, last + 1, step)]


class RefuelRuleTests(SimpleTestCase):
    def test_25_percent_left_refuels_at_375_miles_each_time(self):
        # stations every 100 mi; trigger at mile 375 -> window 375-425 holds only mile 400
        r = refuel_rule(every(100, 900), 1000, 3.0, LEFT, 50.0, R, MPG)
        self.assertEqual(r.stops, 2)  # miles 400 and 800; at 800 the finish is within reach
        self.assertAlmostEqual(r.cost, 1000 / MPG * 3.0)  # flat price: just the fuel burnt

    def test_25_percent_used_refuels_much_more_often(self):
        # trigger at 125, nothing in 125-175, so it takes the nearest reachable station: 200
        r = refuel_rule(every(100, 900), 1000, 3.0, USED, 50.0, R, MPG)
        self.assertEqual(r.stops, 3)  # 200, 400, 600; from 600 the finish (400 mi) is within reach of a full tank
        self.assertAlmostEqual(r.cost, 300.0)

    def test_picks_the_cheapest_station_in_the_window(self):
        cands = [cand(380, 3.50), cand(400, 3.00), cand(420, 3.20)]
        r = refuel_rule(cands, 600, 4.00, LEFT, 50.0, R, MPG)
        self.assertEqual(r.stops, 1)
        # first in, first out: the whole 500 mi start tank (4.00) is burnt before any fuel bought at 3.00,
        # so 400 mi to the stop + the 100 mi left in the start tank, then the last 100 mi on 3.00 fuel
        self.assertAlmostEqual(r.cost, 500 / MPG * 4.00 + 100 / MPG * 3.00)

    def test_no_stop_when_the_destination_is_in_reach(self):
        r = refuel_rule(every(100, 400), 450, 3.2, LEFT, 50.0, R, MPG)
        self.assertEqual(r.stops, 0)
        self.assertAlmostEqual(r.cost, 45 * 3.2)

    def test_detour_burns_fuel_and_is_paid_for(self):
        r = refuel_rule([cand(400, 3.00, off=5.0)], 600, 4.00, LEFT, 50.0, R, MPG)
        self.assertEqual(r.stops, 1)
        self.assertAlmostEqual(r.gallons, 600 / MPG + 1.0)  # 10 mi round trip / 10 mpg
        # 610 mi burnt in all (600 + the 10 mi round trip): the first 500 on the 4.00 start tank, the rest on 3.00 fuel
        self.assertAlmostEqual(r.cost, 500 / MPG * 4.00 + 110 / MPG * 3.00)

    def test_runs_dry_when_no_station_can_be_reached(self):
        self.assertIsNone(refuel_rule([cand(100, 3.0)], 900, 3.0, LEFT, 50.0, R, MPG))
        self.assertIsNone(refuel_rule([], 800, 3.0, LEFT, 50.0, R, MPG))

    def test_cannot_reach_pumps_that_are_too_far_off_the_route(self):
        # at mile 375 only 125 mi remain; pumps 130 mi off the road are out of reach
        self.assertIsNone(refuel_rule([cand(375, 3.0, off=130.0)], 800, 3.0, LEFT, 50.0, R, MPG))

    def test_the_optimal_plan_never_costs_more_than_either_rule(self):
        cands = [cand(m, 2.8 + (m * 37 % 13) / 10) for m in range(40, 2400, 40)]
        best = plan_fuel(cands, 2500, 3.5, R, MPG, 10.0)
        for level in (USED, LEFT):
            r = refuel_rule(cands, 2500, 3.5, level, 50.0, R, MPG)
            self.assertGreaterEqual(r.cost + 1e-6, best.total_cost)
