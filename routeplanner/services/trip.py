"""Orchestrates one request: resolve places -> route -> candidates -> plan -> payload."""

import dataclasses
import time
from urllib.parse import urlencode

from django.conf import settings

from . import navigation, planner
from .geo import haversine_miles
from .places import resolve_place
from .routing import RoutingError, describe, driven_detours, get_route
from .stations import get_station_index


def _place_json(place):
    out = {
        "query": place.query,
        "label": place.label,
        "lat": round(place.lat, 5),
        "lon": round(place.lon, 5),
        "resolved_by": place.source,
    }
    if place.resolved_address:
        out["resolved_address"] = place.resolved_address
    if place.alternatives:
        out["alternatives"] = list(place.alternatives)
    return out


def _station_json(station):
    return {
        "station_id": station.id,
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "lat": round(station.lat, 5),
        "lon": round(station.lon, 5),
        "location": station.location_source,
    }


def plan_trip(start_text, finish_text):
    """Return the full JSON-serialisable response for a start/finish pair.

    Raises LocationError / GeocodingUnavailable / RoutingError / NoRouteFound /
    planner.NoFeasiblePlan, which the view maps to HTTP statuses.
    """
    t0 = time.perf_counter()
    cfg = settings.FUEL_PLANNER

    start = resolve_place(start_text)
    finish = resolve_place(finish_text)
    routed, routing_calls = get_route(
        (start.lat, start.lon), (finish.lat, finish.lon), cfg["ROUTE_SPACING_MILES"]
    )
    route = routed.route
    total_miles = routed.distance_miles

    stations = get_station_index()
    candidates = planner.find_candidates(route, stations, cfg["SEARCH_RADIUS_MILES"], total_miles)
    ref = planner.start_reference(
        stations, start.lat, start.lon, cfg["START_PRICE_RADIUS_MILES"], haversine_miles
    )
    if ref is None:
        raise planner.NoFeasiblePlan(0.0)
    ref_station, ref_distance = ref

    def make_plan(cands):
        return planner.plan_fuel(
            cands,
            total_miles,
            start_price=ref_station.price,
            range_miles=cfg["RANGE_MILES"],
            mpg=cfg["MPG"],
            stop_penalty=cfg["STOP_PENALTY_USD"],
        )

    plan = make_plan(candidates)

    # The plan above used straight-line detours. One more routing call measures the real driven
    # detour to the chosen stops and their likely stand-ins; then we plan again with those.
    detour_calls, driven = 0, False
    calls_so_far = routing_calls + start.external_calls + finish.external_calls
    if cfg["DRIVEN_DETOURS"] and plan.stops and calls_so_far < cfg["MAX_EXTERNAL_CALLS"]:
        checks = planner.detour_checks(plan, candidates, cfg["DETOUR_CHECK_LIMIT"])
        try:
            measured, detour_calls = driven_detours(route, checks, cfg["DETOUR_SPAN_MILES"])
        except RoutingError:
            measured, detour_calls = {}, 1  # keep the straight-line plan rather than fail the request
        if measured:
            driven = True
            candidates = [
                dataclasses.replace(c, detour=measured[c.station.id]) if c.station.id in measured else c
                for c in candidates
            ]
            plan = make_plan(candidates)

    stops_json = []
    for order, p in enumerate(plan.stops, 1):
        c = p.candidate
        stops_json.append(
            {
                "order": order,
                **_station_json(c.station),
                "price_per_gallon": round(p.price, 3),
                "mile_marker": round(c.mile, 1),
                "miles_off_route": round(c.off_route, 1),  # straight line
                "detour_miles": round(2 * c.off, 2),  # round trip charged for: driven if measured
                "detour_source": "straight_line" if c.detour is None else "driven",
                "gallons": round(p.gallons, 2),  # bought here
                "detour_gallons": round(p.detour_gallons, 2),  # burnt driving to and from these pumps
                "cost": round(p.cost, 2),
            }
        )

    query = urlencode({"start": start_text, "finish": finish_text})
    return {
        "start": _place_json(start),
        "finish": _place_json(finish),
        "route": {
            "distance_miles": round(total_miles, 1),
            "duration_hours": round(routed.duration_hours, 2),
            "geometry": route.geojson_linestring(),
        },
        "routing": describe(),  # engine, vehicle (car or truck, with its size) and the credit line
        "start_fill": {
            "description": (
                "Fuel from the full starting tank that the trip actually uses, priced at the "
                "cheapest station near the origin."
            ),
            "reference_station": {
                **_station_json(ref_station),
                "miles_from_origin": round(ref_distance, 1),
            },
            "price_per_gallon": round(plan.start.price, 3),
            "gallons": round(plan.start.gallons, 2),
            "cost": round(plan.start.cost, 2),
        },
        "fuel_stops": stops_json,
        "summary": {
            "total_gallons": round(plan.total_gallons, 2),  # distance / mpg + detour_gallons
            "detour_gallons": round(plan.detour_gallons, 2),
            "total_fuel_cost": round(plan.total_cost, 2),
            "num_fuel_stops": len(stops_json),
            "range_miles": cfg["RANGE_MILES"],
            "mpg": cfg["MPG"],
            "candidate_stations_considered": len(candidates),
            "stop_penalty_usd": cfg["STOP_PENALTY_USD"],
            "driven_detours": driven,  # False: the detour measurement was off, skipped or failed
        },
        "map_url": f"/api/route/map/?{query}",
        "navigation": {  # the plan in a navigation app; see services/navigation.py
            "google_maps": navigation.google_maps_links(
                start_text, finish_text, stops_json, navigation.DESKTOP_WAYPOINTS
            ),
            "google_maps_mobile": navigation.google_maps_links(
                start_text, finish_text, stops_json, navigation.MOBILE_WAYPOINTS
            ),
            "gpx_url": f"/api/route/gpx/?{query}",
        },
        "api_calls": {
            "routing": routing_calls + detour_calls,
            "geocoding": start.external_calls + finish.external_calls,
        },
        "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
