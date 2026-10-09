"""Handing the plan to a navigation app: Google Maps links and the GPX file."""

import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

from django.test import SimpleTestCase

from routeplanner.services import navigation as nav

NS = {"g": nav.GPX_NS}


def stop(order, location="site", name=None, city="Elko", state="NV"):
    return {
        "order": order, "name": name or f"STOP {order}", "address": f"I-80, Exit {order}", "city": city,
        "state": state, "lat": 40.0 + order / 100, "lon": -115.0 - order / 100, "location": location,
        "price_per_gallon": 3.0 + order / 100, "gallons": 40.0 + order, "cost": 130.0 + order,
    }


def parts(url):
    """(origin, [waypoints], destination) of a Google Maps directions URL."""
    q = parse_qs(urlparse(url).query)
    return q["origin"][0], q["waypoints"][0].split("|") if "waypoints" in q else [], q["destination"][0]


def route_of(links):
    """The sequence of places the links visit, with the shared boundary between parts counted once."""
    seq = []
    for i, link in enumerate(links):
        origin, waypoints, destination = parts(link["url"])
        seq += ([origin] if i == 0 else []) + waypoints + [destination]
    return seq


class GoogleMapsLinkTests(SimpleTestCase):
    def test_one_link_with_every_stop_as_a_waypoint(self):
        stops = [stop(i) for i in (1, 2, 3)]
        (link,) = nav.google_maps_links("Los Angeles, CA", "New York, NY", stops, 9)
        self.assertEqual(link["label"], "Open in Google Maps")
        origin, waypoints, destination = parts(link["url"])
        self.assertEqual((origin, destination), ("Los Angeles, CA", "New York, NY"))
        self.assertEqual(waypoints, ["40.01000,-115.01000", "40.02000,-115.02000", "40.03000,-115.03000"])
        q = parse_qs(urlparse(link["url"]).query)
        self.assertEqual((q["api"], q["travelmode"]), (["1"], ["driving"]))
        self.assertIn("%7C", link["url"])  # Google asks for the waypoint separator to be encoded

    def test_no_stops_means_no_waypoints(self):
        (link,) = nav.google_maps_links("Denver, CO", "Boulder, CO", [], 9)
        q = parse_qs(urlparse(link["url"]).query)
        self.assertNotIn("waypoints", q)
        self.assertEqual((q["origin"], q["destination"]), (["Denver, CO"], ["Boulder, CO"]))

    def test_a_stop_still_at_its_city_centre_is_sent_by_name(self):
        stops = [stop(1, location="city", name="AKAL TRAVEL CENTER", city="Waco", state="NE"), stop(2, location="exit")]
        (link,) = nav.google_maps_links("A", "B", stops, 9)
        self.assertEqual(parts(link["url"])[1], ["AKAL TRAVEL CENTER, Waco, NE", "40.02000,-115.02000"])

    def test_a_station_the_sources_dispute_is_sent_by_name_like_a_city_guess(self):
        stops = [stop(1, location="uncertain", name="SPEEDWAY #7175", city="Memphis", state="TN"), stop(2)]
        (link,) = nav.google_maps_links("A", "B", stops, 9)
        self.assertEqual(parts(link["url"])[1], ["SPEEDWAY #7175, Memphis, TN", "40.02000,-115.02000"])

    def test_special_characters_survive_and_a_pipe_cannot_split_a_waypoint(self):
        stops = [stop(1, location="city", name="PUMP & PANTRY | #15", city="Grand Island", state="NE")]
        (link,) = nav.google_maps_links("A", "B", stops, 9)
        self.assertIn("%26", link["url"])
        self.assertEqual(parts(link["url"])[1], ["PUMP & PANTRY   #15, Grand Island, NE"])

    def test_mobile_limit_splits_the_trip_into_parts_that_join_up(self):
        stops = [stop(i) for i in range(1, 8)]  # 7 stops + the finish = 8 targets, 4 per link
        links = nav.google_maps_links("Start", "Finish", stops, nav.MOBILE_WAYPOINTS)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0]["label"], "Part 1 of 2: the start to stop 4")
        self.assertEqual(links[1]["label"], "Part 2 of 2: stop 4 to the finish")
        for link in links:
            self.assertLessEqual(len(parts(link["url"])[1]), nav.MOBILE_WAYPOINTS)
        # part 2 starts exactly where part 1 ended, and nothing is skipped or repeated
        self.assertEqual(parts(links[1]["url"])[0], parts(links[0]["url"])[2])
        expected = ["Start"] + [f"{s['lat']:.5f},{s['lon']:.5f}" for s in stops] + ["Finish"]
        self.assertEqual(route_of(links), expected)

    def test_desktop_limit_of_nine_waypoints(self):
        stops = [stop(i) for i in range(1, 12)]  # 11 stops: 9 waypoints + a destination, then the rest
        links = nav.google_maps_links("S", "F", stops, nav.DESKTOP_WAYPOINTS)
        self.assertEqual(len(links), 2)
        self.assertEqual(len(parts(links[0]["url"])[1]), 9)
        self.assertEqual(route_of(links), ["S"] + [f"{s['lat']:.5f},{s['lon']:.5f}" for s in stops] + ["F"])

    def test_urls_stay_within_googles_2048_character_limit(self):
        long = "X" * 150
        stops = [stop(i, location="city", name=long, city=long) for i in range(1, 10)]
        links = nav.google_maps_links("S", "F", stops, nav.DESKTOP_WAYPOINTS)
        self.assertGreater(len(links), 1)  # too long for one link, so it was split further
        for link in links:
            self.assertLessEqual(len(link["url"]), nav.MAX_URL_CHARS)
        self.assertEqual(route_of(links)[1:-1], [f"{long}, {long}, NV"] * 9)


class GpxTests(SimpleTestCase):
    def payload(self, stops):
        return {
            "start": {"label": "Los Angeles, CA", "lat": 34.0, "lon": -118.4},
            "finish": {"label": "New York, NY", "lat": 40.7, "lon": -73.9},
            "route": {"distance_miles": 2810.7},
            "summary": {"num_fuel_stops": len(stops), "total_fuel_cost": 893.2, "range_miles": 500.0, "mpg": 10.0},
            "fuel_stops": stops,
        }

    def parse(self, stops):
        return ET.fromstring(nav.gpx(self.payload(stops)))

    def test_is_gpx_1_1_with_each_stop_as_a_waypoint_and_a_route_through_all(self):
        root = self.parse([stop(1), stop(2), stop(3)])
        self.assertEqual(root.tag, f"{{{nav.GPX_NS}}}gpx")
        self.assertEqual((root.get("version"), root.get("creator")), ("1.1", "FuelRoute"))
        wpts = root.findall("g:wpt", NS)
        self.assertEqual([w.findtext("g:name", namespaces=NS) for w in wpts], ["1. STOP 1", "2. STOP 2", "3. STOP 3"])
        self.assertAlmostEqual(float(wpts[0].get("lat")), 40.01)
        self.assertAlmostEqual(float(wpts[0].get("lon")), -115.01)
        names = [p.findtext("g:name", namespaces=NS) for p in root.findall("g:rte/g:rtept", NS)]
        self.assertEqual(names, ["Start: Los Angeles, CA", "1. STOP 1", "2. STOP 2", "3. STOP 3", "Finish: New York, NY"])

    def test_elements_come_in_the_order_the_schema_requires(self):
        root = self.parse([stop(1)])
        self.assertEqual([c.tag.split("}")[1] for c in root], ["metadata", "wpt", "rte"])

    def test_waypoint_describes_the_purchase_and_how_sure_the_position_is(self):
        root = self.parse([stop(1, location="city")])
        desc = root.find("g:wpt/g:desc", NS).text
        self.assertIn("$3.010/gal", desc)
        self.assertIn("buy 41.0 gal for $131.00", desc)
        self.assertIn("approximate", desc)
        self.assertIn("at the fuel station", self.parse([stop(1)]).find("g:wpt/g:desc", NS).text.lower())

    def test_a_guessed_position_is_a_waypoint_but_not_a_point_the_gps_must_route_through(self):
        root = self.parse([stop(1), stop(2, location="city"), stop(3, location="exit")])
        self.assertEqual(len(root.findall("g:wpt", NS)), 3)  # still shown, with its warning
        names = [p.findtext("g:name", namespaces=NS) for p in root.findall("g:rte/g:rtept", NS)]
        self.assertEqual(names, ["Start: Los Angeles, CA", "1. STOP 1", "3. STOP 3", "Finish: New York, NY"])
        warning = [w.findtext("g:desc", namespaces=NS) for w in root.findall("g:wpt", NS)][1]
        self.assertIn("left out of the route", warning)

    def test_a_disputed_station_is_a_waypoint_with_a_warning_but_not_a_route_point(self):
        root = self.parse([stop(1), stop(2, location="uncertain"), stop(3)])
        self.assertEqual(len(root.findall("g:wpt", NS)), 3)
        names = [p.findtext("g:name", namespaces=NS) for p in root.findall("g:rte/g:rtept", NS)]
        self.assertEqual(names, ["Start: Los Angeles, CA", "1. STOP 1", "3. STOP 3", "Finish: New York, NY"])
        warning = root.findall("g:wpt", NS)[1].findtext("g:desc", namespaces=NS)
        self.assertIn("disagree", warning)
        self.assertIn("check the name", warning)

    def test_xml_special_characters_are_escaped(self):
        raw = nav.gpx(self.payload([stop(1, name="PUMP & PANTRY <#15>")]))
        self.assertNotIn(b"PUMP & PANTRY", raw)  # the raw ampersand must be escaped in the file
        name = ET.fromstring(raw).find("g:wpt/g:name", NS).text
        self.assertEqual(name, "1. PUMP & PANTRY <#15>")

    def test_a_trip_with_no_stops_is_just_start_and_finish(self):
        root = self.parse([])
        self.assertEqual(root.findall("g:wpt", NS), [])
        self.assertEqual(len(root.findall("g:rte/g:rtept", NS)), 2)
