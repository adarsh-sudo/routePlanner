from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase

from routeplanner.services import places
from routeplanner.services.places import (
    GeocodingUnavailable,
    LocationError,
    _split_city_state,
    normalize_city,
    resolve_place,
)

from .helpers import FakeResponse


class NormalizeTests(SimpleTestCase):
    def test_normalization(self):
        self.assertEqual(normalize_city("Saint Louis"), "ST LOUIS")
        self.assertEqual(normalize_city("St. Louis"), "ST LOUIS")
        self.assertEqual(normalize_city("Mount Pleasant"), "MT PLEASANT")
        self.assertEqual(normalize_city("Cañon City"), "CANON CITY")
        self.assertEqual(normalize_city("Winston-Salem"), "WINSTON SALEM")
        self.assertEqual(normalize_city("  New   Castle   "), "NEW CASTLE")


class SplitTests(SimpleTestCase):
    def test_formats(self):
        self.assertEqual(_split_city_state("Austin, TX"), ("Austin", "TX"))
        self.assertEqual(_split_city_state("Austin, Texas"), ("Austin", "TX"))
        self.assertEqual(_split_city_state("austin tx"), ("austin", "TX"))
        self.assertEqual(_split_city_state("Salt Lake City Utah"), ("Salt Lake City", "UT"))
        self.assertEqual(_split_city_state("Charleston, West Virginia"), ("Charleston", "WV"))
        self.assertEqual(_split_city_state("Austin, TX, USA"), ("Austin", "TX"))

    def test_not_city_state(self):
        self.assertIsNone(_split_city_state("1600 Pennsylvania Ave NW, Washington, DC"))
        self.assertIsNone(_split_city_state("Texas"))
        self.assertIsNone(_split_city_state("Austin, Narnia"))


class NonUsTests(SimpleTestCase):
    def test_canadian_and_mexican_input_is_rejected_without_a_lookup(self):
        cases = ["Calgary, AB", "Toronto, Ontario", "Vancouver BC", "Montreal, QC, Canada", "Tijuana, Mexico"]
        with mock.patch.object(places.requests, "get") as get:
            for text in cases:
                with self.assertRaises(LocationError, msg=text):
                    resolve_place(text)
        get.assert_not_called()

    def test_us_places_that_look_similar_are_not_rejected(self):
        for text in ("Orlando, FL", "Ontario, CA", "Ontario, California", "Albany, NY"):
            self.assertEqual(resolve_place(text).source, "gazetteer", text)

    def test_places_qualified_with_a_foreign_country_are_rejected_without_a_lookup(self):
        with mock.patch.object(places.requests, "get") as get:
            for text in ("Paris, France", "Mumbai, India", "Sydney, Australia", "Berlin, Germany"):
                with self.assertRaises(LocationError, msg=text):
                    resolve_place(text)
        get.assert_not_called()

    def test_bare_country_name_with_no_us_place_of_that_name_is_rejected(self):
        with mock.patch.object(places.requests, "get") as get:
            for text in ("United Kingdom", "New Zealand", "Saudi Arabia"):
                with self.assertRaises(LocationError, msg=text):
                    resolve_place(text)
        get.assert_not_called()

    def test_country_named_towns_resolve_offline_because_they_are_us_places(self):
        # India is a hamlet in four states, Brazil a town in six: real US places, so they resolve.
        with mock.patch.object(places.requests, "get") as get:
            india = resolve_place("India")
            brazil = resolve_place("brazil")
        get.assert_not_called()
        self.assertEqual((india.source, india.external_calls), ("gazetteer", 0))
        self.assertEqual({india.label, *india.alternatives}, {"India, MS", "India, PA", "India, TN", "India, TX"})
        self.assertIn("one of 4 US places named India", india.resolved_address)
        self.assertEqual(brazil.label.split(", ")[0], "Brazil")
        self.assertIn("Brazil, IN", {brazil.label, *brazil.alternatives})

    def test_a_state_picks_the_exact_place(self):
        for text, state in (("India, PA", "PA"), ("India, TX", "TX"), ("Lebanon, TN", "TN"), ("Peru, IN", "IN")):
            p = resolve_place(text)
            self.assertEqual((p.source, p.label.split(", ")[1], p.alternatives), ("gazetteer", state, ()), text)
        self.assertAlmostEqual(resolve_place("India, PA").lat, 40.376, delta=0.01)  # a GNIS community
        self.assertAlmostEqual(resolve_place("India, TX").lat, 32.525, delta=0.01)

    def test_bare_city_name_resolves_offline_to_the_biggest_place(self):
        with mock.patch.object(places.requests, "get") as get:
            chicago = resolve_place("Chicago")
            slc = resolve_place("salt lake city")
        get.assert_not_called()
        self.assertEqual(chicago.label, "Chicago, IL")
        self.assertEqual(slc.label, "Salt Lake City, UT")

    def test_a_bare_name_means_the_most_populous_place_not_the_biggest_by_land_area(self):
        # Land area ranked Miami, KS (a 558-person township) above Miami, FL (~490,000), Atlanta, ID above
        # Atlanta, GA, Cleveland, NE above Cleveland, OH. Population gets all of these right.
        with mock.patch.object(places.requests, "get") as get:
            labels = {name: resolve_place(name).label for name in (
                "Miami", "Atlanta", "Cleveland", "Buffalo", "Newark", "Oakland", "Salem", "Lincoln", "Albany", "Rochester")}
        get.assert_not_called()
        self.assertEqual(labels, {
            "Miami": "Miami, FL", "Atlanta": "Atlanta, GA", "Cleveland": "Cleveland, OH", "Buffalo": "Buffalo, NY",
            "Newark": "Newark, NJ", "Oakland": "Oakland, CA", "Salem": "Salem, OR", "Lincoln": "Lincoln, NE",
            "Albany": "Albany, NY", "Rochester": "Rochester, NY",
        })

    def test_the_ambiguity_note_still_lists_the_other_places_in_population_order(self):
        miami = resolve_place("miami")
        self.assertEqual(miami.label, "Miami, FL")
        self.assertIn("one of 11 US places named Miami", miami.resolved_address)
        self.assertEqual(miami.alternatives[0], "Miami, OH")  # the Ohio township (~50,000) is next
        self.assertNotIn("Miami, FL", miami.alternatives)

    def test_naming_the_state_still_picks_that_state(self):
        self.assertEqual(resolve_place("Miami, KS").label, "Miami, KS")
        self.assertAlmostEqual(resolve_place("Miami, FL").lat, 25.775, delta=0.01)
        self.assertAlmostEqual(resolve_place("Miami, FL").lon, -80.209, delta=0.01)


class ResolveTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        patcher = mock.patch.object(places, "NOMINATIM_MIN_INTERVAL", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_coordinates_need_no_lookup(self):
        for text in ("39.7392,-104.9903", "39.7392, -104.9903", "39.7392 -104.9903"):
            p = resolve_place(text)
            self.assertEqual((p.lat, p.lon, p.source, p.external_calls), (39.7392, -104.9903, "coordinates", 0))

    def test_city_state_resolves_offline(self):
        with mock.patch.object(places.requests, "get") as get:
            p = resolve_place("Denver, CO")
        get.assert_not_called()
        self.assertEqual(p.source, "gazetteer")
        self.assertEqual(p.external_calls, 0)
        self.assertAlmostEqual(p.lat, 39.76, delta=0.1)
        self.assertAlmostEqual(p.lon, -104.88, delta=0.1)

    def test_state_name_variant_and_common_spellings(self):
        for text in ("Saint Louis, Missouri", "St. Louis, MO", "Boise, ID", "Miami, FL", "New York, NY"):
            self.assertEqual(resolve_place(text).source, "gazetteer", text)

    def test_address_goes_to_nominatim_once_then_is_cached(self):
        payload = [{"lat": "38.8977", "lon": "-77.0365", "display_name": "White House, DC"}]
        with mock.patch.object(places.requests, "get", return_value=FakeResponse(payload)) as get:
            a = resolve_place("1600 Pennsylvania Ave NW, Washington, DC")
            b = resolve_place("1600 Pennsylvania Ave NW, Washington, DC")
        self.assertEqual(get.call_count, 1)
        self.assertEqual((a.external_calls, b.external_calls), (1, 0))
        self.assertAlmostEqual(a.lat, 38.8977)
        # The label stays the user's own words; the geocoder's match is separate.
        self.assertEqual(a.label, "1600 Pennsylvania Ave NW, Washington, DC")
        self.assertEqual(a.resolved_address, "White House, DC")

    def test_unknown_city_falls_back_to_nominatim(self):
        payload = [{"lat": "35.5", "lon": "-100.5", "display_name": "Somewhere, TX"}]
        with mock.patch.object(places.requests, "get", return_value=FakeResponse(payload)) as get:
            p = resolve_place("Nowheresville, TX")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(p.source, "nominatim")

    def test_weak_free_text_match_is_rejected(self):
        # Real Nominatim answers: "xyz" -> a trail in Georgia, importance 0.04.
        trail = [{"lat": "34.77", "lon": "-85.14", "display_name": "XYZ, Whitfield County, Georgia", "importance": 0.04}]
        with mock.patch.object(places.requests, "get", return_value=FakeResponse(trail)):
            with self.assertRaises(LocationError):
                resolve_place("xyz")

    def test_well_known_landmark_is_accepted(self):
        # Not a settlement, so not in the gazetteer: it goes to the geocoder and passes on importance.
        rushmore = [{"lat": "43.88", "lon": "-103.46", "display_name": "Mount Rushmore, SD", "importance": 0.55}]
        with mock.patch.object(places.requests, "get", return_value=FakeResponse(rushmore)):
            self.assertEqual(resolve_place("Mount Rushmore").source, "nominatim")

    def test_a_name_unknown_to_the_gazetteer_never_resolves_to_a_us_place(self):
        # No US settlement is called "xyz": it is judged by the geocoder match alone.
        self.assertEqual(places.lookup_name("xyz"), [])

    def test_street_addresses_and_zips_are_not_gated_on_importance(self):
        zip_code = [{"lat": "34.09", "lon": "-118.41", "display_name": "90210, Los Angeles", "importance": 0.12}]
        with mock.patch.object(places.requests, "get", return_value=FakeResponse(zip_code)):
            self.assertEqual(resolve_place("90210").source, "nominatim")

    def test_errors(self):
        for bad in ("", "   ", None, 12):
            with self.assertRaises(LocationError):
                resolve_place(bad)
        with self.assertRaises(LocationError):
            resolve_place("95.0, 10.0")  # latitude out of range
        with self.assertRaises(LocationError):
            resolve_place("51.05, -114.07")  # Calgary: valid coords, not in the USA
        with mock.patch.object(places.requests, "get", return_value=FakeResponse([])):
            with self.assertRaises(LocationError):
                resolve_place("Zzyzx Qwerty 99999")

    def test_geocoder_failure_is_reported(self):
        with mock.patch.object(places.requests, "get", side_effect=places.requests.ConnectionError("boom")):
            with self.assertRaises(GeocodingUnavailable):
                resolve_place("12 Main Street, Springfield")
