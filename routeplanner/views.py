import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .services import navigation, routing
from .services.places import GeocodingUnavailable, LocationError
from .services.planner import NoFeasiblePlan
from .services.routing import NoRouteFound, RouteTooLong, RoutingError
from .services.stations import StationsNotLoaded
from .services.trip import plan_trip

USAGE = {
    "name": "Fuel-optimized route planner",
    "endpoints": {
        "GET|POST /api/route/": "JSON: route geometry, optimal fuel stops, total fuel cost",
        "GET /api/route/map/": "HTML map of the same result (Leaflet)",
        "GET /api/route/gpx/": "GPX file of the stops and route, for a GPS or navigation app",
    },
    "parameters": {
        "start": "'lat,lon', 'City, ST', or any US address",
        "finish": "same formats as start",
    },
    "example": "/api/route/?start=Los Angeles, CA&finish=New York, NY",
}


# Shown under an error on the map page: how to fix it, by error code (first) or HTTP status.
ERROR_HINTS = {
    "route_too_long": "Split the trip in two: plan to a city part-way along, then from there to the finish.",
    400: "Enter a US city and state like Denver, CO, a street address, or latitude and longitude.",
    422: "The fuel data has too few stations along this route. Try a different start or finish.",
    502: "The routing service didn't answer. Wait a moment and try again.",
    503: "The station data isn't loaded yet. Run `python manage.py load_stations`, then reload.",
}


def _error(status, code, message):
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def _read_params(request):
    """start/finish from the query string or (POST) a JSON body."""
    if request.method == "POST" and request.content_type == "application/json":
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            raise LocationError("Request body is not valid JSON.")
        if not isinstance(body, dict):
            raise LocationError("JSON body must be an object with 'start' and 'finish'.")
        return body.get("start"), body.get("finish")
    source = request.POST if request.method == "POST" else request.GET
    return source.get("start"), source.get("finish")


def _compute(request):
    """Returns (payload, None) or (None, (status, code, message))."""
    try:
        start, finish = _read_params(request)
        if not start or not finish:
            raise LocationError("Both 'start' and 'finish' are required.")
        return plan_trip(start, finish), None
    except LocationError as exc:
        return None, (400, "invalid_location", str(exc))
    except RouteTooLong as exc:
        return None, (422, "route_too_long", str(exc))
    except (NoFeasiblePlan, NoRouteFound) as exc:
        return None, (422, "no_feasible_route", str(exc))
    except (RoutingError, GeocodingUnavailable) as exc:
        return None, (502, "upstream_unavailable", str(exc))
    except StationsNotLoaded as exc:
        return None, (503, "stations_not_loaded", str(exc))


def index(request):
    return JsonResponse(USAGE)


@require_http_methods(["GET", "HEAD", "POST"])
def route_api(request):
    payload, err = _compute(request)
    return _error(*err) if err else JsonResponse(payload)


@require_http_methods(["GET", "HEAD"])
def route_warm(request):
    """Open the connection to the routing service ahead of a search; the map page calls this when the
    user starts typing, which hides the ~0.8 s handshake. Does nothing if it was used recently."""
    routing.warm()
    return HttpResponse(status=204)


@require_http_methods(["GET", "HEAD"])
def route_gpx(request):
    """The plan as a GPX file (stops as waypoints, start -> stops -> finish as a route)."""
    payload, err = _compute(request)
    if err:
        return _error(*err)
    response = HttpResponse(navigation.gpx(payload), content_type="application/gpx+xml")
    response["Content-Disposition"] = 'attachment; filename="fuelroute.gpx"'
    return response


@require_http_methods(["GET", "HEAD"])
def route_map(request):
    """The map page. With no inputs it shows just the form; errors are shown in the page."""
    start = request.GET.get("start", "").strip()
    finish = request.GET.get("finish", "").strip()
    payload = err = None
    if start or finish:
        payload, err = _compute(request)
    context = {
        "payload": payload, "start": start, "finish": finish,
        "error": err[2] if err else None, "error_hint": (ERROR_HINTS.get(err[1]) or ERROR_HINTS.get(err[0])) if err else None,
    }
    return render(request, "routeplanner/map.html", context, status=err[0] if err else 200)
