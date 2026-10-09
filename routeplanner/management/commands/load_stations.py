"""Load the fuel price CSV into the Station table (offline; no network calls)."""

from collections import Counter

from django.core.management.base import BaseCommand

from routeplanner.models import Station
from routeplanner.services import places
from routeplanner.services.stations import dedupe_by_stop, read_fuel_csv, read_station_coords


class Command(BaseCommand):
    help = (
        "Parse the fuel price CSV and (re)populate Station: real positions from station_coords.csv "
        "where known, otherwise the city centre (offline)."
    )

    def handle(self, *args, **opts):
        if not places.PLACES_CSV.exists():
            self.stderr.write("us_places.csv is missing - run `python manage.py build_places` first.")
            return

        rows = list(read_fuel_csv())
        stops = dedupe_by_stop(rows)
        real = read_station_coords()

        stations, ungeocoded, by_source = [], Counter(), Counter()
        for stop in stops:
            lat, lon, source = real.get(stop["opis_id"]) or (None, None, "city")
            if lat is None:
                city = places.lookup_city(stop["city"], stop["state"])
                if city is None:
                    ungeocoded[(stop["city"], stop["state"])] += 1
                    continue
                lat, lon = city
            by_source[source] += 1
            stations.append(
                Station(
                    opis_id=stop["opis_id"],
                    name=stop["name"],
                    address=stop["address"],
                    city=stop["city"],
                    state=stop["state"],
                    latitude=lat,
                    longitude=lon,
                    location_source=source,
                    price=stop["price"],
                )
            )

        Station.objects.all().delete()
        Station.objects.bulk_create(stations, batch_size=1000)

        self.stdout.write(
            self.style.SUCCESS(
                f"{len(rows):,} US price rows -> {len(stops):,} unique stops -> "
                f"{len(stations):,} loaded; {sum(ungeocoded.values())} skipped (no coordinates)."
            )
        )
        self.stdout.write(
            "Positions: "
            + ", ".join(f"{by_source[k]:,} {label}" for k, label in
                        (("site", "at the station"), ("uncertain", "uncertain station"),
                         ("exit", "at the exit"), ("city", "city centre")))
        )
        if ungeocoded:
            self.stdout.write(f"Ungeocoded cities: {sorted(ungeocoded)[:25]}")
