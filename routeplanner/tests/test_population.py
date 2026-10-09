"""Census population estimates, used to rank same-named places (see build_places)."""

from django.test import SimpleTestCase

from routeplanner.management.commands.build_places import population_from_rows


def row(sumlev, name, state, pop, year="POPESTIMATE2025"):
    return {"SUMLEV": sumlev, "NAME": name, "STNAME": state, year: str(pop), "POPESTIMATE2024": "1"}


class PopulationFromRowsTests(SimpleTestCase):
    def test_names_are_normalised_like_the_gazetteer_and_the_state_is_abbreviated(self):
        pop = population_from_rows([
            row("162", "Miami city", "Florida", 489812),
            row("162", "Saint Louis city", "Missouri", 280000),
            row("162", "Miami town", "Arizona", 1514),
        ])
        self.assertEqual(pop[("MIAMI", "FL")], 489812)
        self.assertEqual(pop[("MIAMI", "AZ")], 1514)
        self.assertEqual(pop[("ST LOUIS", "MO")], 280000)  # the gazetteer's own form, so "St." and "Saint" both find it

    def test_townships_count_and_the_biggest_of_several_namesakes_wins(self):
        pop = population_from_rows([
            row("061", "Miami township", "Ohio", 5008),
            row("061", "Miami township", "Ohio", 52991),
            row("061", "Miami township", "Kansas", 558),
        ])
        self.assertEqual(pop[("MIAMI", "OH")], 52991)
        self.assertEqual(pop[("MIAMI", "KS")], 558)

    def test_only_places_townships_and_consolidated_cities_are_used(self):
        pop = population_from_rows([
            row("050", "Miami-Dade County", "Florida", 2800000),  # a county
            row("040", "Florida", "Florida", 23000000),  # a state
            row("157", "Miami city", "Florida", 1),  # a county's share of a place
            row("162", "Hialeah city", "Florida", 230000),
        ])
        self.assertEqual(pop, {("HIALEAH", "FL"): 230000})

    def test_a_consolidated_government_is_findable_by_its_city_name(self):
        pop = population_from_rows([row("170", "Louisville/Jefferson County metro government (balance)", "Kentucky", 630000)])
        self.assertEqual(pop[("LOUISVILLE", "KY")], 630000)

    def test_the_latest_year_is_used_and_bad_values_are_skipped(self):
        pop = population_from_rows([
            {"SUMLEV": "162", "NAME": "Aville city", "STNAME": "Texas", "POPESTIMATE2024": "10", "POPESTIMATE2026": "99"},
            {"SUMLEV": "162", "NAME": "Bville city", "STNAME": "Texas", "POPESTIMATE2025": ""},
            {"SUMLEV": "162", "NAME": "Cville city", "STNAME": "Puerto Rico", "POPESTIMATE2025": "5"},
        ])
        self.assertEqual(pop, {("AVILLE", "TX"): 99})
