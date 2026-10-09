"""Regenerate the README's comparison of the optimal plan with the simple "refuel at 25%" rules.

All three are run on the same stations, with the same straight-line detours, 500 mile range, 10 mpg
and fuel-start model (see ``services/baselines.py`` for the rule). It calls the live routing
service once per route, so it needs the network. Prints a Markdown table.

Usage:
  python manage.py compare_rules
  python manage.py compare_rules --routes "Denver, CO|Chicago, IL" "Boston, MA|Dallas, TX"
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from routeplanner.services import planner
from routeplanner.services.baselines import refuel_rule
from routeplanner.services.geo import haversine_miles
from routeplanner.services.places import resolve_place
from routeplanner.services.routing import get_route
from routeplanner.services.stations import get_station_index

ROUTES = ["Seattle, WA|Miami, FL", "Los Angeles, CA|New York, NY", "Boston, MA|Dallas, TX", "Denver, CO|Chicago, IL"]
WINDOW_MILES = 50.0


class Command(BaseCommand):
    help = "Compare the optimal plan with 'refuel when 25% of the tank is used / left' on a few routes."

    def add_arguments(self, parser):
        parser.add_argument("--routes", nargs="+", default=ROUTES, metavar="START|FINISH")

    def handle(self, *args, **opts):
        cfg = settings.FUEL_PLANNER
        R, mpg = cfg["RANGE_MILES"], cfg["MPG"]
        stations = get_station_index()
        lines = ["| Route | Optimal | Refuel at 25% used | Refuel at 25% left |", "|---|---|---|---|"]
        for spec in opts["routes"]:
            a, b = (p.strip() for p in spec.split("|"))
            start, finish = resolve_place(a), resolve_place(b)
            routed, _ = get_route((start.lat, start.lon), (finish.lat, finish.lon), cfg["ROUTE_SPACING_MILES"])
            total = routed.distance_miles
            cands = planner.find_candidates(routed.route, stations, cfg["SEARCH_RADIUS_MILES"], total)
            ref, _ = planner.start_reference(
                stations, start.lat, start.lon, cfg["START_PRICE_RADIUS_MILES"], haversine_miles
            )
            plan = planner.plan_fuel(cands, total, ref.price, R, mpg, cfg["STOP_PENALTY_USD"])
            cells = [f"${plan.total_cost:,.2f}, {len(plan.stops)} stops"]
            for level in (0.75 * R, 0.25 * R):
                r = refuel_rule(cands, total, ref.price, level, WINDOW_MILES, R, mpg)
                if r is None:
                    cells.append("cannot finish")
                else:
                    extra = (r.cost / plan.total_cost - 1) * 100
                    cells.append(f"${r.cost:,.2f}, {r.stops} stops (+{extra:.1f}%)")
            label = f"{a.split(',')[0]} -> {b.split(',')[0]}"
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        self.stdout.write("\n".join(lines))
