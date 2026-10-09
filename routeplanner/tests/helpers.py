"""Shared test helpers."""

from routeplanner.services.geo import haversine_miles
from routeplanner.services.stations import StationRec


def encode_polyline(points, precision=6):
    """Inverse of geo.decode_polyline (Google/OSRM polyline algorithm)."""
    factor = 10 ** precision
    out, plat, plon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def straight_line(start, finish, step_deg=0.02):
    """Densely sampled straight 'route' between two (lat, lon) points."""
    n = max(2, int(max(abs(finish[0] - start[0]), abs(finish[1] - start[1])) / step_deg))
    return [
        (start[0] + (finish[0] - start[0]) * i / n, start[1] + (finish[1] - start[1]) * i / n)
        for i in range(n + 1)
    ]


def fake_osrm_payload(points):
    length = sum(
        haversine_miles(*points[i], *points[i + 1]) for i in range(len(points) - 1)
    )
    return {
        "code": "Ok",
        "routes": [
            {
                "geometry": encode_polyline(points),
                "distance": length * 1609.344,
                "duration": length / 60 * 3600,
            }
        ],
    }


def fake_osrm_table(url, params, circuity=1.3):
    """OSRM /table answer for the coordinates in ``url``: driven meters = straight line x ``circuity``."""
    coords = [tuple(map(float, p.split(","))) for p in url.split("/driving/")[1].split(";")]  # (lon, lat)
    rows = [int(i) for i in params["sources"].split(";")]
    cols = [int(i) for i in params["destinations"].split(";")]

    def meters(a, b):
        return haversine_miles(a[1], a[0], b[1], b[0]) * 1609.344 * circuity

    return {"code": "Ok", "distances": [[meters(coords[i], coords[j]) for j in cols] for i in rows]}


def fake_ors_route(points):
    """What OpenRouteService answers to a directions request along ``points`` (5-digit polyline, metres, seconds)."""
    length = sum(haversine_miles(*points[i], *points[i + 1]) for i in range(len(points) - 1))
    return {
        "routes": [
            {
                "summary": {"distance": length * 1609.344, "duration": length / 55 * 3600},
                "geometry": encode_polyline(points, precision=5),
            }
        ]
    }


def fake_ors_matrix(body, circuity=1.3):
    """OpenRouteService matrix answer for a request body: driven metres = straight line x ``circuity``."""
    locs = body["locations"]  # [lon, lat]

    def meters(a, b):
        return haversine_miles(a[1], a[0], b[1], b[0]) * 1609.344 * circuity

    rows = [int(i) for i in body["sources"]]
    cols = [int(i) for i in body["destinations"]]
    return {"distances": [[meters(locs[i], locs[j]) for j in cols] for i in rows]}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def station(sid, lat, lon, price, city="Testville", state="TX", location="city"):
    return StationRec(sid, f"Stop {sid}", "I-0, EXIT 1", city, state, lat, lon, price, location)
