"""One-off data pull: fuel and convenience-store listings from Overture Maps, near the truck stops.

OpenStreetMap lacks many fuel stations (Maverik #674 in North Las Vegas is not on it at all). Overture
Maps (https://overturemaps.org) publishes open business listings, partly from Foursquare, that include
store numbers in their names. ``build_station_coords`` uses them as a second source of stations.

This reads Overture's GeoParquet files on S3 with DuckDB and writes

  routeplanner/data/overture_places.csv.gz     id, name, tax, status, lat, lon
  routeplanner/data/overture_places.release    which release it came from

keeping only places within ``--miles`` of a truck stop's current position. Overture data is
CDLA-Permissive-2.0; the Places theme combines several sources (e.g. Foursquare, Apache-2.0).

DuckDB is needed only for this command, not to run the app:  pip install duckdb

Usage:
  python manage.py fetch_overture_places                 # latest release, ~4 minutes
  python manage.py fetch_overture_places --release 2026-09-23.1
"""

import csv
import gzip
import json
import math
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from django.core.management.base import BaseCommand, CommandError

from routeplanner.services import places
from routeplanner.services.stations import DATA_DIR, dedupe_by_stop, read_fuel_csv, read_station_coords

STAC_CATALOG_URL = "https://stac.overturemaps.org/catalog.json"
OUT_CSV = DATA_DIR / "overture_places.csv.gz"
OUT_RELEASE = DATA_DIR / "overture_places.release"
CELL = 0.1  # degrees, as in station_match.SiteIndex

# Fuel stops come labelled gas_station, truck_stop, truck_gas_station or (for a shop that sells fuel)
# convenience_store. The category is only a coarse filter; matching is by name.
QUERY = """
COPY (
  SELECT id, names.primary AS name, taxonomy.primary AS tax, operating_status AS status,
         round(bbox.ymin, 5) AS lat, round(bbox.xmin, 5) AS lon
  FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*', hive_partitioning=1)
  WHERE bbox.xmin BETWEEN -125.1 AND -66.8 AND bbox.ymin BETWEEN 24.3 AND 49.6
    AND names.primary IS NOT NULL
    AND (basic_category IN ('gas_station', 'fueling_station', 'convenience_store')
         OR taxonomy.primary IN ('truck_stop', 'truck_gas_station', 'gas_station', 'convenience_store', 'fueling_station'))
) TO '{out}' (HEADER, DELIMITER ',')
"""


def latest_release():
    with urlopen(STAC_CATALOG_URL, timeout=30) as resp:
        return json.load(resp)["latest"]


class Command(BaseCommand):
    help = "Download fuel/convenience places from Overture Maps that lie near the truck stops."

    def add_arguments(self, parser):
        parser.add_argument("--release", help="Overture release, e.g. 2026-09-23.1 (default: the latest).")
        parser.add_argument("--miles", type=float, default=12.0, help="Keep places this near a stop (default 12).")

    def handle(self, *args, **opts):
        try:
            import duckdb
        except ImportError as exc:
            raise CommandError("This command needs DuckDB:  pip install duckdb") from exc

        release = opts["release"] or latest_release()
        self.stdout.write(f"Overture release {release}: reading fuel and convenience places for the US (a few minutes)...")
        start = time.time()
        con = duckdb.connect()
        con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "places.csv"
            con.execute(QUERY.format(release=release, out=str(raw).replace("\\", "/")))
            self.stdout.write(f"  downloaded in {time.time() - start:.0f}s")
            wanted = self._cells_near_stops(opts["miles"])
            kept, total = 0, 0
            with open(raw, encoding="utf-8", newline="") as fh, gzip.open(OUT_CSV, "wt", encoding="utf-8", newline="") as out:
                writer = csv.writer(out)
                writer.writerow(["id", "name", "tax", "status", "lat", "lon"])
                for row in csv.DictReader(fh):
                    total += 1
                    if (int(float(row["lat"]) // CELL), int(float(row["lon"]) // CELL)) in wanted:
                        writer.writerow([row["id"], row["name"], row["tax"], row["status"], row["lat"], row["lon"]])
                        kept += 1
        OUT_RELEASE.write_text(f"{release}\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"Kept {kept:,} of {total:,} places within {opts['miles']:.0f} miles of a truck stop -> {OUT_CSV.name} "
            f"(release {release}). Now run `build_station_coords --offline`."
        ))

    def _cells_near_stops(self, miles):
        """Grid cells within ``miles`` of any truck stop's current best position (exact if known, else its city)."""
        real = read_station_coords()
        d_lat = miles / 69.0
        cells = set()
        for stop in dedupe_by_stop(read_fuel_csv()):
            pos = real.get(stop["opis_id"], (None,))
            if pos[0] is not None:
                lat, lon = pos[0], pos[1]
            else:
                city = places.lookup_city(stop["city"], stop["state"])
                if city is None:
                    continue
                lat, lon = city
            d_lon = d_lat / max(0.2, math.cos(math.radians(lat)))
            for i in range(int((lat - d_lat) // CELL), int((lat + d_lat) // CELL) + 1):
                for j in range(int((lon - d_lon) // CELL), int((lon + d_lon) // CELL) + 1):
                    cells.add((i, j))
        return cells
