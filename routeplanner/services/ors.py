"""OpenRouteService (https://openrouteservice.org): the truck-routing engine.

This module only builds requests and reads answers; ``routing`` does the HTTP. Two services are used, both with
the ``driving-hgv`` profile (heavy goods vehicle, which is how ORS says "truck"):

* ``POST /v2/directions/driving-hgv``: the route. Coordinates go in as ``[lon, lat]``. The answer is JSON with
  ``routes[0].geometry`` (an encoded polyline of 5 decimal digits) and ``routes[0].summary`` (``distance`` in
  metres, ``duration`` in seconds).
* ``POST /v2/matrix/driving-hgv``: driven distances between many points, for the detour check.

The API key goes in an ``Authorization`` header (see ``routing``), never in the URL.
"""

from .geo import decode_polyline

PROFILE = "driving-hgv"
ROUTE_PATH = f"/v2/directions/{PROFILE}"
MATRIX_PATH = f"/v2/matrix/{PROFILE}"
CREDIT = "openrouteservice.org by HeiGIT"  # ORS asks to be credited; its map data is OpenStreetMap's
# A detour check sends 3 coordinates per station and asks for (2 x stations) x (2 x stations) distances. The free
# plan allows 3,500 distances a request; 16 stations is 48 locations and 1,024 distances.
MAX_MATRIX_STATIONS = 16


def restrictions(truck):
    """The ``restrictions`` block for the route request, from ``settings.TRUCK``. Unset sizes are left out."""
    sizes = {
        "height": truck["HEIGHT_M"], "width": truck["WIDTH_M"], "length": truck["LENGTH_M"],
        "weight": truck["WEIGHT_T"], "axleload": truck["AXLE_LOAD_T"],
    }
    out = {name: round(value, 2) for name, value in sizes.items() if value}
    out["hazmat"] = bool(truck["HAZMAT"])
    return out


def route_body(start, finish, truck):
    """Request body for the route from ``start`` to ``finish`` (both ``(lat, lon)``)."""
    return {
        "coordinates": [[round(start[1], 6), round(start[0], 6)], [round(finish[1], 6), round(finish[0], 6)]],
        # Snap to the nearest road however far away (the default is 350 m, and a city's centre point can be
        # farther than that from the road network). OSRM does the same.
        "radiuses": [-1, -1],
        "instructions": False,  # we only need the line, not turn-by-turn text
        "options": {"vehicle_type": "hgv", "profile_params": {"restrictions": restrictions(truck)}},
    }


def parse_route(data):
    """``(points, meters, seconds)`` from a directions answer, ``points`` being ``(lat, lon)`` pairs.

    Raises ValueError if the answer holds no route.
    """
    try:
        route = data["routes"][0]
        summary = route["summary"]
        return decode_polyline(route["geometry"], precision=5), summary["distance"], summary["duration"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("the answer holds no route") from exc


def matrix_body(coords, stations):
    """Request body for the detour matrix.

    ``coords`` is every A point, then every S (station) point, then every B point, each ``(lat, lon)``, for
    ``stations`` stations. Sources are the A's and S's, destinations the S's and B's, so the answer holds
    d(A,S), d(A,B) and d(S,B) for each station at row/column i, stations+i.
    """
    m = stations
    return {
        "locations": [[round(lon, 6), round(lat, 6)] for lat, lon in coords],
        "sources": [str(i) for i in range(2 * m)],
        "destinations": [str(i) for i in range(m, 3 * m)],
        "metrics": ["distance"],
        "units": "m",
    }


def parse_matrix(data, stations):
    """The ``2m x 2m`` rows of metres (``None`` where a pair has no route) from a matrix answer.

    Raises ValueError if the answer is not that shape.
    """
    try:
        rows = data["distances"]
        if len(rows) != 2 * stations or any(len(row) != 2 * stations for row in rows):
            raise ValueError("the matrix is not the size that was asked for")
        return rows
    except (KeyError, TypeError) as exc:
        raise ValueError("the answer holds no distances") from exc


def explain(status, data):
    """``(kind, message)`` for a request that did not return 200.

    ``kind`` is ``"no_route"`` (the places cannot be joined), ``"too_long"`` (over the plan's distance limit) or
    ``"error"`` (anything else). The message is safe to show to a user.
    """
    err = data.get("error") if isinstance(data, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    text = str((err.get("message") if isinstance(err, dict) else err) or "").strip()

    if code in (2009, 2010):  # route could not be found / point not found near a road
        return "no_route", "No drivable route found between those locations."
    if code == 2004 and "distance" in text.lower():
        return "too_long", (
            "That trip is longer than the OpenRouteService free plan allows (6,000 km, about 3,700 miles)."
        )
    if status in (401, 403):
        return "error", (
            "OpenRouteService refused the request: check ORS_API_KEY, or today's free quota may be used up."
        )
    if status == 429:
        return "error", "OpenRouteService's rate limit was reached (40 requests a minute on the free plan)."
    detail = f"{code or status} {text}".strip()
    return "error", f"OpenRouteService error: {detail}"[:240]
