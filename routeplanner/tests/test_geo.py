from django.test import SimpleTestCase

from routeplanner.services.geo import Route, decode_polyline, haversine_miles, in_conus

from .helpers import encode_polyline, straight_line


class HaversineTests(SimpleTestCase):
    def test_known_distance(self):
        # Los Angeles -> New York great-circle is ~2,445 miles.
        d = haversine_miles(34.0522, -118.2437, 40.7128, -74.0060)
        self.assertAlmostEqual(d, 2445, delta=10)

    def test_zero(self):
        self.assertEqual(haversine_miles(10, 10, 10, 10), 0)


class PolylineTests(SimpleTestCase):
    def test_google_reference_vector(self):
        # Canonical example from Google's polyline documentation (precision 5).
        pts = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
        expected = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
        for (la, lo), (ela, elo) in zip(pts, expected):
            self.assertAlmostEqual(la, ela, places=5)
            self.assertAlmostEqual(lo, elo, places=5)

    def test_roundtrip_precision6(self):
        pts = [(34.052235, -118.243683), (36.1699, -115.1398), (40.712776, -74.005974)]
        decoded = decode_polyline(encode_polyline(pts, 6), 6)
        for a, b in zip(pts, decoded):
            self.assertAlmostEqual(a[0], b[0], places=6)
            self.assertAlmostEqual(a[1], b[1], places=6)


class RouteTests(SimpleTestCase):
    def setUp(self):
        # 0.003 deg ~ 0.17 mi between raw points, so 0.5 mi thinning really thins.
        self.raw = straight_line((35.0, -100.0), (35.0, -90.0), step_deg=0.003)
        self.route = Route(self.raw, spacing_miles=0.5)

    def test_thinning_keeps_endpoints_and_length(self):
        self.assertEqual(self.route.points[0], self.raw[0])
        self.assertEqual(self.route.points[-1], self.raw[-1])
        full = sum(haversine_miles(*a, *b) for a, b in zip(self.raw, self.raw[1:]))
        self.assertAlmostEqual(self.route.length_miles, full, places=6)
        self.assertLess(len(self.route.points), len(self.raw))

    def test_miles_monotonic(self):
        m = self.route.miles
        self.assertTrue(all(b >= a for a, b in zip(m, m[1:])))

    def test_nearest_finds_mile_marker_and_offset(self):
        # A point ~5 miles north of the route, midway along it.
        lat = 35.0 + 5 / 69.0935
        idx, dist = self.route.nearest(lat, -95.0, max_miles=10)
        self.assertAlmostEqual(dist, 5, delta=0.3)
        self.assertAlmostEqual(self.route.miles[idx], self.route.length_miles / 2, delta=1.0)

    def test_nearest_respects_radius(self):
        self.assertIsNone(self.route.nearest(36.0, -95.0, max_miles=10))  # ~69 mi away

    def test_bbox_contains_route(self):
        lat_min, lat_max, lon_min, lon_max = self.route.bbox(10)
        self.assertLess(lat_min, 35.0)
        self.assertGreater(lat_max, 35.0)
        self.assertLess(lon_min, -100.0)
        self.assertGreater(lon_max, -90.0)

    def test_geojson_is_lon_lat(self):
        first = self.route.geojson_linestring()["coordinates"][0]
        self.assertEqual(first, [-100.0, 35.0])


class BoundsTests(SimpleTestCase):
    def test_in_conus(self):
        self.assertTrue(in_conus(39.7, -104.9))
        self.assertFalse(in_conus(51.0, -114.0))  # Calgary
        self.assertFalse(in_conus(19.4, -99.1))  # Mexico City
