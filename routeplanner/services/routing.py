"""Routing client: one HTTP call returns the full driving route, a second measures real detours.

Two engines, chosen by ``settings.ROUTING_ENGINE``: OSRM (free, keyless, plans car routes) and OpenRouteService
(``ors``: plans truck routes, needs a free API key; see ``ors.py``). Everything above this module is the same
for both: ``get_route``, ``driven_detours`` and ``warm``.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import requests
from django.conf import settings

from . import ors
from .geo import Route, decode_polyline

METERS_PER_MILE = 1609.344
CACHE_MAX_ROUTES = 256
CACHE_MAX_DETOURS = 5000
MAX_TABLE_COORDS = 100  # the public OSRM server answers "TooBig" for more

_session = requests.Session()  # keep-alive across requests
_cache = OrderedDict()  # key -> RouteResult (LRU; no pickling, so hits are ~free)
_detour_cache = OrderedDict()  # (engine, A, station, B) rounded coordinates -> extra round-trip miles
_cache_lock = threading.Lock()
_throttle_lock = threading.Lock()
_last_call_at = 0.0


class RoutingError(RuntimeError):
    """The routing service failed (HTTP 502)."""


class NoRouteFound(RoutingError):
    """The routing service found no drivable route (HTTP 422)."""


class RouteTooLong(RoutingError):
    """The trip is longer than the routing service allows (HTTP 422)."""


@dataclass(frozen=True)
class RouteResult:
    route: Route  # thinned polyline with mile markers + spatial grid
    distance_miles: float  # as reported by the routing service
    duration_hours: float


def _engine():
    return settings.ROUTING_ENGINE


def describe():
    """What the routes are planned for: the engine, the vehicle, and the credit to show on the page."""
    if _engine() == "ors":
        t = settings.TRUCK
        truck = {
            "height_m": t["HEIGHT_M"], "width_m": t["WIDTH_M"], "length_m": t["LENGTH_M"],
            "weight_t": t["WEIGHT_T"], "hazmat": bool(t["HAZMAT"]),
        }
        return {"engine": "openrouteservice", "vehicle": "truck", "credit": ors.CREDIT, "truck": truck, "note": None}
    return {
        "engine": "osrm", "vehicle": "car", "credit": "OSRM", "truck": None,
        # shown on the page: a truck planner must not quietly hand out a car route
        "note": "This is a car route, not a truck route. Add an OpenRouteService key (ORS_API_KEY) to plan for a truck.",
    }


def _min_interval():
    return settings.ORS_MIN_INTERVAL_SECONDS if _engine() == "ors" else settings.OSRM_MIN_INTERVAL_SECONDS


def _timeout():
    return settings.ORS_TIMEOUT_SECONDS if _engine() == "ors" else settings.OSRM_TIMEOUT_SECONDS


def _throttle():
    """Keep calls to the routing server at least the engine's minimum interval apart (OSRM's public server asks for 1 s)."""
    global _last_call_at
    gap = _min_interval()
    with _throttle_lock:
        wait = _last_call_at + gap - time.monotonic()
        if gap > 0 and wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _ors_post(path, body):
    """POST ``body`` to OpenRouteService and return the parsed answer.

    Raises NoRouteFound / RouteTooLong / RoutingError. The key travels in a header, so it can appear in
    neither a URL nor an error message.
    """
    _throttle()
    try:
        resp = _session.post(
            settings.ORS_BASE_URL + path,
            json=body,
            headers={
                "Authorization": settings.ORS_API_KEY,
                "Accept": "application/json",
                "User-Agent": settings.HTTP_USER_AGENT,
            },
            timeout=_timeout(),
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingError(f"Routing service unavailable: {exc}") from exc
    if resp.status_code != 200:
        kind, message = ors.explain(resp.status_code, data)
        raise {"no_route": NoRouteFound, "too_long": RouteTooLong}.get(kind, RoutingError)(message)
    return data


def _fetch_osrm(start, finish):
    """``(points, meters, seconds)`` from the public OSRM server."""
    url = (
        f"{settings.OSRM_BASE_URL}/route/v1/driving/"
        f"{start[1]:.6f},{start[0]:.6f};{finish[1]:.6f},{finish[0]:.6f}"
    )
    _throttle()
    try:
        resp = _session.get(
            url,
            params={"overview": "full", "geometries": "polyline6", "steps": "false"},
            headers={"User-Agent": settings.HTTP_USER_AGENT},
            timeout=_timeout(),
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingError(f"Routing service unavailable: {exc}") from exc

    code = data.get("code")
    if code in ("NoRoute", "NoSegment"):
        raise NoRouteFound("No drivable route found between those locations.")
    if resp.status_code != 200 or code != "Ok" or not data.get("routes"):
        detail = f"{code or resp.status_code} {data.get('message', '')}".strip()
        raise RoutingError(f"Routing service error: {detail}")

    best = data["routes"][0]
    return decode_polyline(best["geometry"], precision=6), best["distance"], best["duration"]


def _fetch_ors(start, finish):
    """``(points, meters, seconds)`` from OpenRouteService, for the truck in ``settings.TRUCK``."""
    data = _ors_post(ors.ROUTE_PATH, ors.route_body(start, finish, settings.TRUCK))
    try:
        return ors.parse_route(data)
    except ValueError as exc:
        raise RoutingError(f"Routing service error: {exc}") from exc


def _fetch(start, finish, spacing_miles):
    points, meters, seconds = (_fetch_ors if _engine() == "ors" else _fetch_osrm)(start, finish)
    return RouteResult(
        route=Route(points, spacing_miles=spacing_miles),
        distance_miles=meters / METERS_PER_MILE,
        duration_hours=seconds / 3600,
    )


def get_route(start, finish, spacing_miles):
    """Return ``(RouteResult, network_calls_made)`` for (lat, lon) -> (lat, lon).

    Cached by rounded coordinates: a repeated query makes no network call.
    """
    key = (_engine(), round(start[0], 4), round(start[1], 4), round(finish[0], 4), round(finish[1], 4), spacing_miles)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit, 0

    result = _fetch(start, finish, spacing_miles)
    with _cache_lock:
        _cache[key] = result
        while len(_cache) > CACHE_MAX_ROUTES:
            _cache.popitem(last=False)
    return result, 1


WARM_IDLE_SECONDS = 35  # the public server keeps an idle connection for 45 s or more; refresh before that


def warm():
    """Open (or keep open) the connection to the routing server, so the next real call skips its handshake.

    The first call on a new connection took ~1.0 s against ~0.2 s on one already open, and an idle
    connection stayed usable for 45 s but had closed by 90 s. Does nothing if the server was used
    in the last ``WARM_IDLE_SECONDS``. Returns True if it made a request. The request goes to the server's
    front page without the API key, so it cannot use up any quota.
    """
    if _last_call_at and time.monotonic() - _last_call_at < WARM_IDLE_SECONDS:
        return False
    _throttle()  # it is still a request to the server
    base = settings.ORS_BASE_URL if _engine() == "ors" else settings.OSRM_BASE_URL
    try:
        _session.get(base + "/", headers={"User-Agent": settings.HTTP_USER_AGENT}, timeout=_timeout())
    except requests.RequestException:
        pass  # warming is best effort: the real call will report any problem
    return True


def _table_osrm(coords, stations):
    """The ``2m x 2m`` driven-distance rows (metres) for ``coords`` from the OSRM table service."""
    m = stations
    path = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    params = {
        "annotations": "distance",
        "sources": ";".join(str(i) for i in range(2 * m)),
        "destinations": ";".join(str(i) for i in range(m, 3 * m)),
    }
    _throttle()
    try:
        resp = _session.get(
            f"{settings.OSRM_BASE_URL}/table/v1/driving/{path}",
            params=params,
            headers={"User-Agent": settings.HTTP_USER_AGENT},
            timeout=_timeout(),
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingError(f"Routing service unavailable: {exc}") from exc
    if resp.status_code != 200 or data.get("code") != "Ok" or "distances" not in data:
        raise RoutingError(f"Routing service error: {data.get('code') or resp.status_code}")
    return data["distances"]


def _table_ors(coords, stations):
    """The same rows from the OpenRouteService matrix service (truck profile)."""
    data = _ors_post(ors.MATRIX_PATH, ors.matrix_body(coords, stations))
    try:
        return ors.parse_matrix(data, stations)
    except ValueError as exc:
        raise RoutingError(f"Routing service error: {exc}") from exc


def driven_detours(route, candidates, span_miles):
    """Real extra driving miles of a round trip from the route to each candidate station.

    One table (matrix) call, however many stations. For a station S, A and B are the route points
    ``span_miles`` before and after where S meets the route. Driving A -> S -> B instead of A -> B
    costs d(A,S) + d(S,B) - d(A,B) extra miles, the stretch shared with the highway cancels, so
    it does not matter that the driver may leave and rejoin at different exits.

    Returns ``({station_id: extra_round_trip_miles}, calls_made)``. Stations the service could not
    route to are left out. Raises RoutingError if the call fails.
    """
    engine = _engine()
    found, missing = {}, []
    with _cache_lock:
        for c in candidates:
            mile = route.miles[c.route_idx]
            trip = (
                route.points[route.index_at(mile - span_miles)],
                (c.station.lat, c.station.lon),
                route.points[route.index_at(mile + span_miles)],
            )
            key = (engine,) + tuple(round(v, 4) for point in trip for v in point)
            if key in _detour_cache:
                _detour_cache.move_to_end(key)
                found[c.station.id] = _detour_cache[key]
            else:
                missing.append((c.station.id, trip, key))
    missing = missing[: ors.MAX_MATRIX_STATIONS if engine == "ors" else MAX_TABLE_COORDS // 3]
    if not missing:
        return found, 0

    # Coordinates: all the A's, then all the S's, then all the B's. Sources are A and S, destinations
    # S and B, so the matrix holds d(A,S), d(A,B) and d(S,B) for every station (row/column i, m+i).
    m = len(missing)
    coords = [t[k] for k in range(3) for _, t, _ in missing]
    dist = (_table_ors if engine == "ors" else _table_osrm)(coords, m)

    with _cache_lock:
        for i, (station_id, _, key) in enumerate(missing):
            a_to_s, a_to_b, s_to_b = dist[i][i], dist[i][m + i], dist[m + i][m + i]
            if None in (a_to_s, a_to_b, s_to_b):
                continue
            extra = round(max(0.0, a_to_s + s_to_b - a_to_b) / METERS_PER_MILE, 3)
            found[station_id] = _detour_cache[key] = extra
        while len(_detour_cache) > CACHE_MAX_DETOURS:
            _detour_cache.popitem(last=False)
    return found, 1


def clear_cache():
    with _cache_lock:
        _cache.clear()
        _detour_cache.clear()
