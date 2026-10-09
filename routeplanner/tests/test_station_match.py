from django.test import SimpleTestCase

from routeplanner.services import station_match as sm

# One junction on I-44 (exit 283, shared with US-69) and one on an unnumbered state road.
OVERPASS_EXITS = {
    "elements": [
        {"type": "node", "id": 1, "lat": 36.9000, "lon": -95.0000, "tags": {"highway": "motorway_junction", "ref": "283"}},
        {"type": "node", "id": 2, "lat": 36.9500, "lon": -95.1000, "tags": {"highway": "motorway_junction", "ref": "284A"}},
        {"type": "node", "id": 3, "lat": 37.5000, "lon": -95.5000, "tags": {"highway": "motorway_junction", "ref": "9"}},
        {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"highway": "motorway", "ref": "I 44;US 69;OK 3"}},
        {"type": "way", "id": 11, "nodes": [3], "tags": {"highway": "primary", "ref": "OK 9"}},
    ]
}


def fuel(osm_id, lat, lon, name=None, brand=None, **tags):
    tags = {k: v for k, v in {"amenity": "fuel", "name": name, "brand": brand, **tags}.items() if v}
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags}


class ParsingTests(SimpleTestCase):
    def test_parse_address_routes_and_exit(self):
        self.assertEqual(sm.parse_address("I-44, EXIT 283 & US-69"), ({"I 44", "US 69"}, "283"))
        self.assertEqual(sm.parse_address("I-29 & I-80, EXIT 3"), ({"I 29", "I 80"}, "3"))
        self.assertEqual(sm.parse_address("I-10, EXIT 15A"), ({"I 10"}, "15A"))

    def test_parse_address_without_exit_or_interstate(self):
        self.assertEqual(sm.parse_address("US-46"), ({"US 46"}, None))
        self.assertEqual(sm.parse_address("SR-9"), (set(), None))

    def test_route_set_ignores_state_routes(self):
        self.assertEqual(sm.route_set("I 44;US 69;OK 3"), {"I 44", "US 69"})
        self.assertEqual(sm.route_set(None), set())

    def test_exit_matches(self):
        self.assertTrue(sm.exit_matches("283", "283"))
        self.assertTrue(sm.exit_matches("283", "283A"))  # a sub-exit covers the plain number
        self.assertTrue(sm.exit_matches("283", "283A-B"))
        self.assertTrue(sm.exit_matches("283", "282;283"))
        self.assertFalse(sm.exit_matches("283", "28"))
        self.assertFalse(sm.exit_matches("283", "2830"))
        self.assertFalse(sm.exit_matches("283A", "283"))
        self.assertFalse(sm.exit_matches("283", None))


class NameTests(SimpleTestCase):
    def test_tokens_drop_filler_and_apostrophes(self):
        self.assertEqual(sm.name_tokens("PILOT TRAVEL CENTER #1243"), {"pilot"})
        self.assertEqual(sm.name_tokens("Love's Travel Stop"), sm.name_tokens("LOVES TRAVEL STOP #302"))
        self.assertIn("ta", sm.name_tokens("TravelCenters of America"))  # the chain's short name

    def test_store_numbers(self):
        self.assertEqual(sm.store_numbers("PILOT #1243"), {"1243"})
        self.assertEqual(sm.store_numbers("Circle K", None), frozenset())

    def test_score_prefers_matching_store_number(self):
        a = sm.fuel_sites({"elements": [fuel(1, 0, 0, name="Pilot Travel Center #1243", brand="Pilot")]})[0]
        b = sm.fuel_sites({"elements": [fuel(2, 0, 0, name="Pilot Travel Center", brand="Pilot")]})[0]
        t, n = sm.name_tokens("PILOT TRAVEL CENTER #1243"), sm.store_numbers("PILOT TRAVEL CENTER #1243")
        self.assertGreater(sm.name_score(t, n, a), sm.name_score(t, n, b))
        self.assertEqual(sm.name_score(sm.name_tokens("WOODSHED OF BIG CABIN"), frozenset(), a), 0.0)


class ExitTests(SimpleTestCase):
    def test_exits_need_a_numbered_way(self):
        found = sm.exits(OVERPASS_EXITS)
        by_ref = {e.ref: e for e in found}
        self.assertEqual(by_ref["283"].routes, {"I 44", "US 69"})
        self.assertNotIn("9", by_ref)  # sits only on a state route, which is ignored

    def test_find_exit_matches_route_and_number(self):
        found = sm.exits(OVERPASS_EXITS)
        ex = sm.find_exit(found, {"I 44"}, "283", near=(36.9, -95.0))
        self.assertEqual(ex.osm_id, "node/1")
        self.assertIsNone(sm.find_exit(found, {"I 70"}, "283", near=(36.9, -95.0)))
        self.assertIsNone(sm.find_exit(found, {"I 44"}, "999", near=(36.9, -95.0)))

    def test_exit_far_from_its_city_is_rejected(self):
        found = sm.exits(OVERPASS_EXITS)
        self.assertIsNone(sm.find_exit(found, {"I 44"}, "283", near=(40.0, -95.0)))


def place(pid, lat, lon, name, tax="gas_station", status="open"):
    return {"id": pid, "name": name, "lat": lat, "lon": lon, "tax": tax, "status": status}


class OvertureTests(SimpleTestCase):
    def test_closed_and_nameless_places_are_dropped_and_ids_are_kept(self):
        sites = sm.overture_sites([
            place("a", 36.0, -115.0, "Maverik #674", tax="truck_gas_station"),
            place("b", 36.1, -115.1, "Shell", status="permanently_closed"),
            place("c", 36.2, -115.2, "Chevron", status="temporarily_closed"),
            place("d", 36.3, -115.3, ""),
            place("e", 36.4, -115.4, "Circle K", status=""),
        ])
        self.assertEqual([s.osm_id for s in sites], ["overture/a", "overture/e"])
        self.assertEqual(sites[0].numbers, {"674"})
        self.assertTrue(sites[0].truck_friendly)
        self.assertFalse(sites[1].truck_friendly)

    def test_the_same_station_in_both_sources_becomes_one_with_the_numbers_of_both(self):
        osm = sm.fuel_sites({"elements": [fuel(1, 36.19500, -115.14200, name="Maverik", brand="Maverik")]})
        ours = sm.overture_sites([place("x", 36.19512, -115.14181, "Maverik #674")])  # ~100 ft away
        (merged,) = sm.merge_sites(osm + ours)
        self.assertEqual((merged.osm_id, merged.lat), ("node/1", 36.195))  # the first source's id and position win
        self.assertEqual(merged.numbers, {"674"})  # but it learned the store number

    def test_two_different_stores_of_one_chain_stay_apart(self):
        osm = sm.fuel_sites({"elements": [fuel(1, 36.19500, -115.14200, name="Maverik", brand="Maverik")]})
        other = sm.overture_sites([place("y", 36.27000, -115.05000, "Maverik")])  # several miles away
        self.assertEqual(len(sm.merge_sites(osm + other)), 2)

    def test_different_brands_at_the_same_corner_are_not_merged(self):
        shell = sm.fuel_sites({"elements": [fuel(1, 36.19500, -115.14200, name="Shell", brand="Shell")]})
        maverik = sm.overture_sites([place("m", 36.19501, -115.14201, "Maverik #674")])  # a few feet away
        merged = sm.merge_sites(shell + maverik)
        self.assertEqual(len(merged), 2)
        self.assertEqual({s.osm_id for s in merged}, {"node/1", "overture/m"})

    def test_a_site_index_returns_what_is_near_and_skips_the_rest(self):
        sites = sm.fuel_sites({"elements": [fuel(1, 36.00, -115.00, name="A"), fuel(2, 36.05, -115.00, name="B"), fuel(3, 40.0, -100.0, name="C")]})
        index = sm.SiteIndex(sites)
        near = {s.osm_id for s in index.near(36.0, -115.0, 10)}
        self.assertEqual(near, {"node/1", "node/2"})
        self.assertEqual(len(index), 3)

    def test_best_site_works_on_an_index_and_on_a_plain_list(self):
        sites = sm.fuel_sites({"elements": [fuel(1, 36.01, -115.0, name="Pilot Travel Center", brand="Pilot")]})
        for coll in (sites, sm.SiteIndex(sites)):
            self.assertEqual(sm.best_site(coll, "PILOT #12", (36.0, -115.0), 5).osm_id, "node/1")
            self.assertIsNone(sm.best_site(coll, "PILOT #12", (37.0, -115.0), 5))

    def test_openstreetmap_wins_a_tie_unless_overture_is_clearly_nearer(self):
        osm = sm.fuel_sites({"elements": [fuel(1, 36.030, -115.0, name="Speedway", brand="Speedway")]})  # ~2.1 mi away
        close = sm.overture_sites([place("o", 36.015, -115.0, "Speedway")])  # ~1.0 mi away: nearer, but not by 1.5 mi
        self.assertEqual(sm.best_site(osm + close, "SPEEDWAY #9", (36.0, -115.0), 8).osm_id, "node/1")
        much_nearer = sm.overture_sites([place("p", 36.0, -115.0, "Speedway")])  # on the spot: ~2.1 mi nearer
        self.assertEqual(sm.best_site(osm + much_nearer, "SPEEDWAY #9", (36.0, -115.0), 8).osm_id, "overture/p")

    def test_a_matching_store_number_beats_a_nearer_or_better_placed_station(self):
        osm = sm.fuel_sites({"elements": [fuel(1, 36.001, -115.0, name="Maverik", brand="Maverik")]})  # nearest, no number
        numbered = sm.overture_sites([place("n", 36.04, -115.0, "Maverik #674")])  # ~2.8 mi away but it is #674
        self.assertEqual(sm.best_site(osm + numbered, "Maverik #674", (36.0, -115.0), 8).osm_id, "overture/n")

    def test_a_station_only_overture_knows_is_found_at_the_exit(self):
        exits = sm.exits(OVERPASS_EXITS)
        ours = sm.overture_sites([place("z", 36.9012, -95.0011, "Pilot #1243")])
        loc = sm.locate("PILOT TRAVEL CENTER #1243", "I-44, EXIT 283 & US-69", (36.92, -95.01), exits, sm.SiteIndex(ours))
        self.assertEqual((loc.source, loc.osm_id), ("site", "overture/z"))


MEMPHIS = (35.10, -89.97)
SPEEDWAY_STOP = {"opis_id": 7175, "name": "SPEEDWAY #7175", "address": "US-78 & SR-175", "city": "Memphis", "state": "TN"}


def at(miles_north):
    """Latitude this many miles north of the Memphis test point (same longitude)."""
    return MEMPHIS[0] + miles_north / 69.09


class DisagreementTests(SimpleTestCase):
    def state(self, osm, overture, stops=(SPEEDWAY_STOP,)):
        return sm.locate_state(
            list(stops), lambda stop: MEMPHIS, [],
            sm.fuel_sites({"elements": osm}), sm.overture_sites(overture),
        )

    def test_two_sources_naming_different_stores_far_apart_make_the_stop_uncertain(self):
        out = self.state(
            [fuel(1, at(-6), MEMPHIS[1], name="Speedway", brand="Speedway")],  # OpenStreetMap: 6 mi south
            [place("o", at(5), MEMPHIS[1], "Speedway")],  # Overture: a different store 5 mi north
        )
        loc = out[7175]
        self.assertEqual(loc.source, "uncertain")
        self.assertEqual(loc.osm_id, "node/1")  # the position is still the merged pick: OpenStreetMap's

    def test_the_same_store_in_both_sources_is_not_uncertain(self):
        out = self.state(
            [fuel(1, at(-3), MEMPHIS[1], name="Speedway", brand="Speedway")],
            [place("o", at(-3) + 0.0001, MEMPHIS[1], "Speedway #7175")],  # ~40 ft away: the same store
        )
        self.assertEqual(out[7175].source, "site")

    def test_stores_less_than_two_miles_apart_are_not_a_disagreement(self):
        out = self.state(
            [fuel(1, at(-3), MEMPHIS[1], name="Speedway", brand="Speedway")],
            [place("o", at(-1.5), MEMPHIS[1], "Speedway")],  # 1.5 mi from OpenStreetMap's pick
        )
        self.assertEqual(out[7175].source, "site")

    def test_one_source_finding_nothing_is_not_a_disagreement(self):
        only_osm = self.state([fuel(1, at(-3), MEMPHIS[1], name="Speedway", brand="Speedway")], [place("x", at(2), MEMPHIS[1], "Shell")])
        self.assertEqual(only_osm[7175].source, "site")
        only_overture = self.state([fuel(2, at(-3), MEMPHIS[1], name="Shell", brand="Shell")], [place("o", at(4), MEMPHIS[1], "Speedway")])
        self.assertEqual((only_overture[7175].source, only_overture[7175].osm_id), ("site", "overture/o"))

    def test_nothing_is_flagged_without_overture_data(self):
        out = self.state([fuel(1, at(-6), MEMPHIS[1], name="Speedway", brand="Speedway")], [])
        self.assertEqual(out[7175].source, "site")

    def test_a_stop_with_no_station_is_left_unmatched_and_an_unknown_city_is_left_out(self):
        nowhere = dict(SPEEDWAY_STOP, opis_id=1, name="NO SUCH STORE")
        out = sm.locate_state([SPEEDWAY_STOP, nowhere], lambda s: None if s["opis_id"] == 1 else MEMPHIS, [], [], [])
        self.assertNotIn(1, out)  # city unknown: not in the result at all
        self.assertIsNone(out[7175])  # city known, nothing near: stays at the city centre

    def test_disagree_truth_table(self):
        near, far = sm.Located(35.0, -90.0, "site", "a"), sm.Located(35.2, -90.0, "site", "b")  # ~14 mi apart
        self.assertTrue(sm.disagree(near, far))
        self.assertFalse(sm.disagree(near, near))
        self.assertFalse(sm.disagree(near, None))
        self.assertFalse(sm.disagree(None, far))
        self.assertFalse(sm.disagree(near, sm.Located(35.2, -90.0, "exit", "e")))  # an exit point is not a station pick


class LocateTests(SimpleTestCase):
    def setUp(self):
        self.exits = sm.exits(OVERPASS_EXITS)
        self.city = (36.92, -95.01)

    def test_named_station_at_the_exit_gives_a_site(self):
        sites = sm.fuel_sites({"elements": [fuel(5, 36.9010, -95.0010, name="Pilot Travel Center", brand="Pilot")]})
        loc = sm.locate("PILOT TRAVEL CENTER #1243", "I-44, EXIT 283 & US-69", self.city, self.exits, sites)
        self.assertEqual((loc.source, loc.osm_id, loc.lat), ("site", "node/5", 36.9010))

    def test_unnamed_exit_gives_the_exit_itself(self):
        loc = sm.locate("WOODSHED OF BIG CABIN", "I-44, EXIT 283 & US-69", self.city, self.exits, [])
        self.assertEqual((loc.source, loc.osm_id, loc.lat, loc.lon), ("exit", "node/1", 36.9, -95.0))

    def test_station_with_another_name_at_the_exit_is_not_used(self):
        sites = sm.fuel_sites({"elements": [fuel(6, 36.9005, -95.0005, name="Shell", brand="Shell")]})
        loc = sm.locate("PILOT TRAVEL CENTER #1243", "I-44, EXIT 283", self.city, self.exits, sites)
        self.assertEqual(loc.source, "exit")

    def test_station_too_far_from_the_exit_is_not_used(self):
        sites = sm.fuel_sites({"elements": [fuel(7, 36.9, -94.9, name="Pilot", brand="Pilot")]})  # ~5.6 mi east
        loc = sm.locate("PILOT #1243", "I-44, EXIT 283", self.city, self.exits, sites)
        self.assertEqual(loc.source, "exit")

    def test_no_exit_number_uses_a_close_named_station(self):
        sites = sm.fuel_sites({"elements": [fuel(8, 36.93, -95.02, name="Circle K", brand="Circle K")]})
        loc = sm.locate("CIRCLE K #2723664", "US-280", self.city, self.exits, sites)
        self.assertEqual((loc.source, loc.osm_id), ("site", "node/8"))

    def test_no_exit_number_ignores_a_station_in_the_next_town(self):
        sites = sm.fuel_sites({"elements": [fuel(9, 37.05, -95.01, name="Circle K", brand="Circle K")]})  # ~9 mi
        self.assertIsNone(sm.locate("CIRCLE K #2723664", "US-280", self.city, self.exits, sites))

    def test_two_same_brand_stops_without_exits_do_not_share_one_station(self):
        sites = sm.fuel_sites({"elements": [
            fuel(20, 36.93, -95.02, name="Pump & Pantry", brand="Pump & Pantry"),
            fuel(21, 36.95, -95.03, name="Pump & Pantry", brand="Pump & Pantry"),
        ]})
        claimed = set()
        first = sm.locate("PUMP & PANTRY #1", "US-30", self.city, self.exits, sites, claimed)
        claimed.add(first.osm_id)
        second = sm.locate("PUMP & PANTRY #2", "US-30", self.city, self.exits, sites, claimed)
        claimed.add(second.osm_id)
        self.assertNotEqual(first.osm_id, second.osm_id)
        # a third store has no station left to take, so it keeps the city centre rather than reuse one
        self.assertIsNone(sm.locate("PUMP & PANTRY #15", "US-30", self.city, self.exits, sites, claimed))

    def test_stops_at_one_exit_may_share_its_station(self):
        sites = sm.fuel_sites({"elements": [fuel(5, 36.9010, -95.0010, name="Pilot Travel Center", brand="Pilot")]})
        claimed = {"node/5"}  # already taken, but an exit number pins the stop to this exit anyway
        loc = sm.locate("PILOT #1243", "I-44, EXIT 283", self.city, self.exits, sites, claimed)
        self.assertEqual((loc.source, loc.osm_id), ("site", "node/5"))

    def test_has_exit(self):
        self.assertTrue(sm.has_exit("I-44, EXIT 283 & US-69"))
        self.assertFalse(sm.has_exit("US-30 & US-281"))
        self.assertFalse(sm.has_exit("EXIT 5"))  # no highway to pair it with

    def test_unknown_exit_falls_back_to_city_station_or_none(self):
        self.assertIsNone(sm.locate("TA TRAVEL CENTER", "I-70, EXIT 1", self.city, self.exits, []))

    def test_site_from_a_way_uses_its_centre(self):
        way = {"type": "way", "id": 12, "center": {"lat": 36.9002, "lon": -95.0003},
               "tags": {"amenity": "fuel", "name": "Love's Travel Stop"}}
        loc = sm.locate("LOVES TRAVEL STOP #302", "I-44, EXIT 283", self.city, self.exits, sm.fuel_sites({"elements": [way]}))
        self.assertEqual((loc.source, loc.osm_id), ("site", "way/12"))
