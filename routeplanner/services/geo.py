"""Geometry helpers: distances, polyline decoding, route thinning, spatial grid."""

import bisect
import math

EARTH_RADIUS_MILES = 3958.7613
MILES_PER_DEG_LAT = 69.0935

# Rough contiguous-US bounding box, used for cheap "is this in the USA?" validation.
CONUS_BOUNDS = (24.3, 49.6, -125.1, -66.8)  # lat_min, lat_max, lon_min, lon_max


def in_conus(lat, lon):
    lat_min, lat_max, lon_min, lon_max = CONUS_BOUNDS
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def haversine_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def decode_polyline(encoded, precision=6):
    """Decode a Google/OSRM encoded polyline into a list of (lat, lon)."""
    factor = 10 ** precision
    coords = []
    index = lat = lon = 0
    length = len(encoded)
    while index < length:
        for axis in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


class Route:
    """A thinned route polyline with cumulative mile markers and a spatial grid.

    ``points`` are (lat, lon); ``miles[i]`` is the distance along the route to
    ``points[i]``. Points are spaced ~``spacing_miles`` apart (the original
    endpoints are always kept), which bounds the mile-marker error for any
    station projected onto the route to about half that spacing.
    """

    GRID_DEG = 0.15  # ~10 miles

    def __init__(self, raw_points, spacing_miles=0.5):
        if len(raw_points) < 2:
            raise ValueError("route needs at least two points")
        points = [raw_points[0]]
        miles = [0.0]
        total = 0.0
        pending = 0.0
        prev = raw_points[0]
        for pt in raw_points[1:]:
            step = haversine_miles(prev[0], prev[1], pt[0], pt[1])
            total += step
            pending += step
            prev = pt
            if pending >= spacing_miles:
                points.append(pt)
                miles.append(total)
                pending = 0.0
        if points[-1] != raw_points[-1]:
            points.append(raw_points[-1])
            miles.append(total)
        self.points = points
        self.miles = miles
        self.length_miles = total
        self._grid = self._build_grid()

    # -- spatial index ---------------------------------------------------
    def _build_grid(self):
        g = self.GRID_DEG
        grid = {}
        for i, (lat, lon) in enumerate(self.points):
            grid.setdefault((int(lat // g), int(lon // g)), []).append(i)
        return grid

    def bbox(self, pad_miles=0.0):
        """(lat_min, lat_max, lon_min, lon_max), padded by ``pad_miles``."""
        lats = [p[0] for p in self.points]
        lons = [p[1] for p in self.points]
        pad_lat = pad_miles / MILES_PER_DEG_LAT
        pad_lon = pad_miles / (MILES_PER_DEG_LAT * math.cos(math.radians(max(abs(min(lats)), abs(max(lats))))))
        return min(lats) - pad_lat, max(lats) + pad_lat, min(lons) - pad_lon, max(lons) + pad_lon

    def nearest(self, lat, lon, max_miles):
        """Return (route_index, distance_miles) of the closest route point within
        ``max_miles`` of (lat, lon), or None.

        Distances use an equirectangular approximation around the query point,
        accurate to well under 1% at these scales.
        """
        g = self.GRID_DEG
        # How many grid cells the search radius spans in each direction.
        lat_cells = int(max_miles / (MILES_PER_DEG_LAT * g)) + 1
        cos_lat = math.cos(math.radians(lat))
        lon_cells = int(max_miles / (MILES_PER_DEG_LAT * cos_lat * g)) + 1
        ci, cj = int(lat // g), int(lon // g)
        miles_per_deg_lon = MILES_PER_DEG_LAT * cos_lat
        best_i, best_d2 = None, max_miles * max_miles
        points = self.points
        grid = self._grid
        for di in range(-lat_cells, lat_cells + 1):
            for dj in range(-lon_cells, lon_cells + 1):
                for idx in grid.get((ci + di, cj + dj), ()):
                    plat, plon = points[idx]
                    dy = (plat - lat) * MILES_PER_DEG_LAT
                    dx = (plon - lon) * miles_per_deg_lon
                    d2 = dx * dx + dy * dy
                    if d2 < best_d2:
                        best_i, best_d2 = idx, d2
        if best_i is None:
            return None
        return best_i, math.sqrt(best_d2)

    def index_at(self, mile):
        """Index of the route point at (or just before) ``mile`` miles along the route, clamped to the ends."""
        return max(0, min(bisect.bisect_right(self.miles, mile) - 1, len(self.points) - 1))

    def geojson_linestring(self):
        # GeoJSON order is [lon, lat].
        return {
            "type": "LineString",
            "coordinates": [[round(lon, 5), round(lat, 5)] for lat, lon in self.points],
        }
