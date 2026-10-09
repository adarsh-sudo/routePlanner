"""Check the OpenRouteService key and the truck-routing path end to end, with two real requests.

    put ORS_API_KEY=your-key in the .env file next to manage.py (copy .env.example), then
    python manage.py check_ors

It plans one short truck route (Dallas to Fort Worth) and then measures one made-up fuel stop beside it, which is
exactly what a search does, so it uses 1 of today's 2,000 route requests and 1 of the 500 matrix requests. It
works whichever engine ``ROUTING_ENGINE`` selects.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from routeplanner.services import ors, routing
from routeplanner.services.planner import Candidate
from routeplanner.services.stations import StationRec

DALLAS = (32.7767, -96.7970)
FORT_WORTH = (32.7555, -97.3308)


class Command(BaseCommand):
    help = "Check the OpenRouteService API key and truck routing with one route and one detour request."

    def handle(self, *args, **options):
        if not settings.ORS_API_KEY:
            raise CommandError(
                "ORS_API_KEY is not set. Get a free key at https://openrouteservice.org/dev/#/signup, then put "
                "ORS_API_KEY=your-key in the .env file next to manage.py (copy .env.example), or set it in the "
                "environment, and run this again."
            )
        truck = ors.restrictions(settings.TRUCK)
        self.stdout.write(f"Truck: {truck}")
        with override_settings(ROUTING_ENGINE="ors"):
            routing.clear_cache()
            try:
                result, _ = routing.get_route(DALLAS, FORT_WORTH, settings.FUEL_PLANNER["ROUTE_SPACING_MILES"])
            except routing.RoutingError as exc:
                raise CommandError(f"Route request failed: {exc}") from exc
            self.stdout.write(self.style.SUCCESS(
                f"Route OK: {result.distance_miles:.1f} miles, {result.duration_hours * 60:.0f} min, "
                f"{len(result.route.points)} points."
            ))

            mid = len(result.route.points) // 2
            lat, lon = result.route.points[mid]
            stop = StationRec(0, "Check stop", "-", "Fort Worth", "TX", lat + 0.01, lon, 3.0, "site")
            try:
                found, _ = routing.driven_detours(result.route, [Candidate(stop, result.route.miles[mid], 0.7, mid)], 10)
            except routing.RoutingError as exc:
                raise CommandError(f"Detour (matrix) request failed: {exc}") from exc
            if 0 not in found:
                raise CommandError("Detour request answered, but without a distance for the check stop.")
            self.stdout.write(self.style.SUCCESS(f"Detour OK: a stop 0.7 miles off the road adds {found[0]:.2f} driven miles."))
        self.stdout.write("OpenRouteService is working. Run the server with ROUTING_ENGINE=ors to plan truck routes.")
