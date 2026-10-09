"""End-to-end API tests with the routing service mocked (no network)."""

import xml.etree.ElementTree as ET
from unittest import mock

import requests
from django.conf import settings
from django.test import TestCase, override_settings

from routeplanner.models import Station
from routeplanner.services import routing, stations
from routeplanner.services.geo import haversine_miles

from .helpers import FakeResponse, fake_osrm_payload, fake_osrm_table, straight_line

START = (35.0, -100.0)
FINISH = (35.0, -85.0)  # ~850 miles east: needs at least one fuel stop


def make_stations():
    rows = [
        # (lon, price) along lat 35.0, plus a couple of decoys
        (-97.0, 3.60),
        (-94.0, 3.20),
        (-91.0, 3.05),
        (-88.0, 3.40),
    ]
    for i, (lon, price) in enumerate(rows, 1):
        Station.objects.create(
            opis_id=i, name=f"Stop {i}", address="I-40, EXIT 1", city="Testville", state="TX",
            latitude=35.0, longitude=lon, price=price, location_source="site",
        )
    Station.objects.create(  # far from the route
        opis_id=99, name="Decoy", address="-", city="Elsewhere", state="MN",
        latitude=46.0, longitude=-93.0, price=1.00,
    )


def planner_settings(**changes):
    """override_settings for the planner tunables, e.g. planner_settings(DRIVEN_DETOURS=False)."""
    return override_settings(FUEL_PLANNER={**settings.FUEL_PLANNER, **changes})


@override_settings(ROUTING_ENGINE="osrm", OSRM_MIN_INTERVAL_SECONDS=0)
class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_stations()

    def setUp(self):
        stations.get_station_index.cache_clear()
        routing.clear_cache()
        self.payload = fake_osrm_payload(straight_line(START, FINISH))
        self.route_response = FakeResponse(self.payload)  # what /route/ answers; tests may swap it
        patcher = mock.patch.object(routing._session, "get", side_effect=self._osrm)
        self.osrm = patcher.start()
        self.addCleanup(patcher.stop)

    def _osrm(self, url, **kwargs):
        if "/table/" in url:
            return FakeResponse(fake_osrm_table(url, kwargs["params"]))
        return self.route_response

    def osrm_calls(self, service):
        return [c for c in self.osrm.call_args_list if f"/{service}/" in c.args[0]]

    def get(self, start="35.0,-100.0", finish="35.0,-85.0", path="/api/route/"):
        return self.client.get(path, {"start": start, "finish": finish})

    # -- happy path -------------------------------------------------------
    def test_a_car_route_says_it_is_one_in_the_data_and_on_the_page(self):
        info = self.get().json()["routing"]
        self.assertEqual((info["engine"], info["vehicle"], info["truck"]), ("osrm", "car", None))
        self.assertIn("not a truck route", info["note"])
        html = self.get(path="/api/route/map/").content.decode()
        self.assertIn('id="car-note"', html)  # the box the page script fills with that sentence...
        self.assertIn('$("car-note").hidden = false;', html)  # ...and un-hides when there is one
        self.assertIn("Routes by OSRM.", html)
        self.assertIn("not a truck route", html)  # the sentence travels in the page's data

    def test_returns_route_stops_and_total_cost(self):
        resp = self.get()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertAlmostEqual(body["route"]["distance_miles"], 850, delta=30)
        self.assertEqual(body["route"]["geometry"]["type"], "LineString")
        self.assertGreater(len(body["route"]["geometry"]["coordinates"]), 10)
        self.assertGreaterEqual(body["summary"]["num_fuel_stops"], 1)
        self.assertEqual(body["map_url"].split("?")[0], "/api/route/map/")

        # total gallons = distance / 10 mpg, plus the fuel burnt driving off the route to the stations
        self.assertAlmostEqual(
            body["summary"]["total_gallons"],
            body["route"]["distance_miles"] / 10 + body["summary"]["detour_gallons"],
            delta=0.06,
        )
        self.assertAlmostEqual(
            body["summary"]["detour_gallons"], sum(s["detour_gallons"] for s in body["fuel_stops"]), delta=0.05
        )
        # total cost = starting tank + every stop
        parts = body["start_fill"]["cost"] + sum(s["cost"] for s in body["fuel_stops"])
        self.assertAlmostEqual(body["summary"]["total_fuel_cost"], parts, delta=0.05)
        gallons = body["start_fill"]["gallons"] + sum(s["gallons"] for s in body["fuel_stops"])
        self.assertAlmostEqual(body["summary"]["total_gallons"], gallons, delta=0.05)

    def test_stops_are_on_route_ordered_and_within_range(self):
        body = self.get().json()
        marks = [0.0] + [s["mile_marker"] for s in body["fuel_stops"]] + [body["route"]["distance_miles"]]
        self.assertEqual(marks, sorted(marks))
        for a, b in zip(marks, marks[1:]):
            self.assertLessEqual(b - a, 500.5)
        for s in body["fuel_stops"]:
            self.assertLessEqual(s["miles_off_route"], 10)
            self.assertNotEqual(s["name"], "Decoy")
            self.assertEqual({"lat", "lon", "price_per_gallon", "gallons", "cost", "city", "state"} - set(s), set())

    def test_cheapest_stations_are_preferred(self):
        names = {s["name"] for s in self.get().json()["fuel_stops"]}
        self.assertIn("Stop 3", names)  # $3.05 - the cheapest on the route

    def test_two_routing_calls_for_coordinate_and_city_input(self):
        # one for the route, one measuring the real detours to the chosen stations
        body = self.get().json()
        self.assertEqual(len(self.osrm_calls("route")), 1)
        self.assertEqual(len(self.osrm_calls("table")), 1)
        self.assertEqual(body["api_calls"], {"routing": 2, "geocoding": 0})

    def test_repeat_request_is_served_from_cache(self):
        self.get()
        body = self.get().json()
        self.assertEqual(self.osrm.call_count, 2)  # nothing new on the second request
        self.assertEqual(body["api_calls"]["routing"], 0)

    # -- navigation hand-off -------------------------------------------------
    def test_response_carries_google_maps_links_and_a_gpx_link(self):
        body = self.get().json()
        nav = body["navigation"]
        stops = body["fuel_stops"]
        (link,) = nav["google_maps"]  # a handful of stops fits one desktop link
        self.assertTrue(link["url"].startswith("https://www.google.com/maps/dir/?api=1"))
        self.assertEqual(link["url"].count("%7C"), len(stops) - 1)  # n waypoints are separated by n-1 pipes
        self.assertGreaterEqual(len(nav["google_maps_mobile"]), 1)
        self.assertTrue(nav["gpx_url"].startswith("/api/route/gpx/?"))
        self.assertEqual(nav["gpx_url"].split("?", 1)[1], body["map_url"].split("?", 1)[1])

    def test_gpx_download_has_every_stop(self):
        stops = self.get().json()["fuel_stops"]
        resp = self.get(path="/api/route/gpx/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/gpx+xml")
        self.assertIn("attachment", resp["Content-Disposition"])
        ns = {"g": "http://www.topografix.com/GPX/1/1"}
        root = ET.fromstring(resp.content)
        self.assertEqual(len(root.findall("g:wpt", ns)), len(stops))
        self.assertEqual(len(root.findall("g:rte/g:rtept", ns)), len(stops) + 2)

    def test_gpx_with_bad_input_is_a_json_error(self):
        resp = self.client.get("/api/route/gpx/", {"start": "35,-100"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_location")

    def test_a_station_the_sources_dispute_is_flagged_and_handled_with_care(self):
        Station.objects.filter(opis_id=3).update(location_source="uncertain")  # Stop 3 is the cheapest, so it is chosen
        stations.get_station_index.cache_clear()
        body = self.get().json()
        stop3 = next(s for s in body["fuel_stops"] if s["name"] == "Stop 3")
        self.assertEqual(stop3["location"], "uncertain")
        self.assertEqual(stop3["detour_source"], "straight_line")  # a doubtful position is not measured by road
        self.assertIn("Stop%203,%20Testville,%20TX", body["navigation"]["google_maps"][0]["url"])  # sent by name
        ns = {"g": "http://www.topografix.com/GPX/1/1"}
        root = ET.fromstring(self.get(path="/api/route/gpx/").content)
        self.assertIn("Stop 3", " ".join(w.findtext("g:name", namespaces=ns) for w in root.findall("g:wpt", ns)))
        route_names = [p.findtext("g:name", namespaces=ns) for p in root.findall("g:rte/g:rtept", ns)]
        self.assertFalse(any("Stop 3" in n for n in route_names))  # a waypoint, but not a point the GPS must reach

    # -- driven detours ----------------------------------------------------
    def test_detours_are_measured_by_road_with_one_extra_call(self):
        body = self.get().json()
        self.assertTrue(body["summary"]["driven_detours"])
        for s in body["fuel_stops"]:
            self.assertEqual(s["detour_source"], "driven")
            self.assertAlmostEqual(s["detour_gallons"], s["detour_miles"] / 10, delta=0.02)

    @planner_settings(DRIVEN_DETOURS=False)
    def test_detours_can_be_switched_off(self):
        body = self.get().json()
        self.assertEqual(self.osrm.call_count, 1)
        self.assertFalse(body["summary"]["driven_detours"])
        for s in body["fuel_stops"]:
            self.assertEqual(s["detour_source"], "straight_line")
            self.assertAlmostEqual(s["detour_miles"], 2 * s["miles_off_route"], delta=0.2)

    @planner_settings(MAX_EXTERNAL_CALLS=1)
    def test_no_detour_call_when_the_call_budget_is_used_up(self):
        body = self.get().json()
        self.assertEqual(len(self.osrm_calls("table")), 0)
        self.assertEqual(body["api_calls"]["routing"], 1)
        self.assertFalse(body["summary"]["driven_detours"])

    def test_failed_detour_call_keeps_the_straight_line_plan(self):
        def respond(url, **kwargs):
            if "/table/" in url:
                raise requests.ConnectionError("table down")
            return self.route_response

        self.osrm.side_effect = respond
        resp = self.get()
        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(body["summary"]["driven_detours"])
        self.assertEqual(body["api_calls"]["routing"], 2)  # the failed attempt still counts
        self.assertGreaterEqual(body["summary"]["num_fuel_stops"], 1)

    def test_city_state_input_makes_no_geocoding_call(self):
        # Real city names resolve offline; routing is mocked, so its geometry is fixed.
        with mock.patch("routeplanner.services.places.requests.get") as geocoder:
            resp = self.get("Oklahoma City, OK", "Memphis, TN")
        self.assertEqual(resp.status_code, 200)
        geocoder.assert_not_called()
        self.assertEqual(resp.json()["api_calls"]["geocoding"], 0)

    def test_post_json(self):
        resp = self.client.post(
            "/api/route/", {"start": "35.0,-100.0", "finish": "35.0,-85.0"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("fuel_stops", resp.json())

    def test_map_page_renders(self):
        resp = self.get(path="/api/route/map/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="trip-data"')
        self.assertContains(resp, "leaflet")

    def test_an_ambiguous_name_means_the_most_populous_place_and_the_others_are_offered(self):
        body = self.get(start="35.0,-100.0", finish="miami").json()
        self.assertEqual(body["finish"]["label"], "Miami, FL")  # not Miami, KS (a 558-person township)
        self.assertAlmostEqual(body["finish"]["lat"], 25.775, delta=0.02)
        self.assertEqual(body["finish"]["alternatives"][0], "Miami, OH")
        self.assertIn("one of 11 US places named Miami", body["finish"]["resolved_address"])
        page = self.get(start="35.0,-100.0", finish="miami", path="/api/route/map/").content.decode()
        self.assertIn("Meant a different one?", page)  # the page turns the alternatives into one-tap links

    def test_map_page_keeps_the_phone_fixes(self):
        # Found by a phone-emulation test in a real browser (not part of this suite): guard them against removal.
        html = self.get(path="/api/route/map/").content.decode()
        self.assertIn("autoPan: true", html)  # a popup must fit the short phone map, heading included
        self.assertIn('position: touch ? "topright" : "bottomright"', html)  # zoom buttons off the pins' band
        self.assertIn("(pointer: coarse) and (max-width: 359px)", html)  # zoom hidden where a popup cannot sit beside it
        self.assertIn("if (window.innerWidth < 900) return;", html)  # a pin tap must not scroll the page away
        self.assertIn('map.once("moveend", open)', html)  # open the popup when the fly-to lands

    def test_map_page_keeps_the_speed_fixes(self):
        # Found by a Lighthouse run (not part of this suite): guard them against removal.
        html = self.get(start="Denver, CO", finish="35.0,-85.0", path="/api/route/map/").content.decode()
        # the result is hidden behind an outline until the script has built it, so the fixed headings never jump down
        self.assertIn('id="results-sk"', html)
        self.assertIn('<section id="results" aria-labelledby="result-title" hidden>', html)
        # Leaflet is fetched from the head without blocking the parser, and the page script waits for it
        self.assertIn('leaflet.min.js" defer></script>', html)
        self.assertIn('document.addEventListener("DOMContentLoaded"', html)
        # the font stylesheet does not hold up the first paint (a <noscript> copy covers no-JS), and tiles are preconnected
        self.assertIn("media=\"print\" onload=\"this.media='all'\"", html)
        self.assertIn('<link rel="preconnect" href="https://server.arcgisonline.com">', html)
        self.assertIn('<meta name="description"', html)

    def test_map_page_has_prefilled_start_and_finish_inputs(self):
        resp = self.get(start="Denver, CO", finish="35.0,-85.0", path="/api/route/map/")
        self.assertContains(resp, 'name="start" value="Denver, CO"')
        self.assertContains(resp, 'name="finish" value="35.0,-85.0"')

    def test_map_page_without_inputs_shows_empty_form(self):
        resp = self.client.get("/api/route/map/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="start" value=""')
        self.assertContains(resp, "<script id=\"trip-data\" type=\"application/json\">null</script>", html=False)
        self.assertEqual(self.osrm.call_count, 0)

    def test_map_page_shows_errors_in_the_page_and_keeps_inputs(self):
        resp = self.get(start="51.05,-114.07", path="/api/route/map/")  # Calgary
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, 'role="alert"', status_code=400)
        self.assertContains(resp, 'value="51.05,-114.07"', status_code=400)
        self.assertEqual(self.osrm.call_count, 0)

    def test_request_url_can_be_made_from_map_url(self):
        body = self.get().json()
        resp = self.client.get(body["map_url"])
        self.assertEqual(resp.status_code, 200)

    # -- errors -----------------------------------------------------------
    def test_missing_params(self):
        for params in ({}, {"start": "35,-100"}, {"finish": "35,-85"}):
            resp = self.client.get("/api/route/", params)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["error"]["code"], "invalid_location")
        self.assertEqual(self.osrm.call_count, 0)

    def test_outside_usa_rejected_before_routing(self):
        resp = self.get(start="51.05,-114.07")  # Calgary
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.osrm.call_count, 0)

    def test_bad_json_body(self):
        resp = self.client.post("/api/route/", "{not json", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_no_route(self):
        self.route_response = FakeResponse({"code": "NoRoute", "message": "Impossible route"})
        resp = self.get()
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"]["code"], "no_feasible_route")

    def test_routing_service_down(self):
        self.osrm.side_effect = requests.ConnectionError("down")
        resp = self.get()
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["error"]["code"], "upstream_unavailable")

    def test_routing_service_http_error(self):
        self.route_response = FakeResponse({"code": "InvalidQuery", "message": "bad"}, status=400)
        self.assertEqual(self.get().status_code, 502)

    def test_unreachable_stretch_is_422(self):
        Station.objects.filter(longitude__gt=-95).delete()  # leaves a >500 mi gap
        stations.get_station_index.cache_clear()
        resp = self.get()
        self.assertEqual(resp.status_code, 422)
        self.assertIn("cannot complete", resp.json()["error"]["message"])

    def test_empty_station_table_is_503_with_instructions(self):
        Station.objects.all().delete()
        stations.get_station_index.cache_clear()
        resp = self.get()
        self.assertEqual(resp.status_code, 503)
        self.assertIn("load_stations", resp.json()["error"]["message"])

    def test_method_not_allowed(self):
        self.assertEqual(self.client.delete("/api/route/").status_code, 405)

    def test_index_documents_the_api(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("endpoints", resp.json())


class DistanceSanityTests(TestCase):
    def test_fake_route_distance_matches_haversine(self):
        pts = straight_line(START, FINISH)
        payload = fake_osrm_payload(pts)
        miles = payload["routes"][0]["distance"] / 1609.344
        self.assertAlmostEqual(miles, haversine_miles(*START, *FINISH), delta=1)
