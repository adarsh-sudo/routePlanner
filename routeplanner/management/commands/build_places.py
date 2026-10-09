"""One-off data build: US place gazetteers + geocodes for station cities they miss.

The fuel CSV has no coordinates, only city/state. This command produces committed
data files so that the app itself rarely geocodes at request time:

  routeplanner/data/us_places.csv                    (US Census Gazetteer, public domain)
  routeplanner/data/us_communities.csv               (USGS GNIS populated places, public domain)
  routeplanner/data/station_geocode_overrides.json   (Nominatim, only for cities the gazetteers miss)

The Census file covers incorporated places, CDPs and townships (~81k names). GNIS adds the
unincorporated communities and hamlets Census has no record of (India PA, Zzyzx CA, ...), so
that essentially every named US settlement is known offline, with coordinates.

Usage:
  python manage.py build_places                    # Census + GNIS, then geocode leftovers
  python manage.py build_places --no-nominatim     # skip the Nominatim pass
  python manage.py build_places --communities-only # only (re)build us_communities.csv from GNIS
  python manage.py build_places --population-only  # only add Census population to us_places.csv
"""

import csv
import io
import json
import re
import time
import zipfile
from collections import defaultdict

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from routeplanner.services import places
from routeplanner.services.stations import read_fuel_csv

CENSUS_BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
PLACES_ZIP = "2024_Gaz_place_national.zip"
COUSUBS_ZIP = "2024_Gaz_cousubs_national.zip"
GNIS_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/DomesticNames/"
    "DomesticNames_National_Text.zip"
)
# Census population estimates for places and townships. Ranks same-named places when a name is typed
# without a state: land area made Miami, KS (a township of ~500 people) beat Miami, FL (~490,000).
POPULATION_URL = (
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/cities/totals/sub-est2025.csv"
)
# 162 incorporated places, 170/172 consolidated cities and their balances, 061 minor civil divisions
# (townships, New England towns). CDPs are not estimated, so they rank by land area behind these.
POPULATION_SUMLEVS = {"162", "170", "172", "061"}

# Census appends a legal-status word to every name ("Dodge City city"). Strip it
# exactly ONCE - stripping repeatedly turns "Dodge City city" into "Dodge".
SUFFIX_RE = re.compile(
    r"\s+(?:(?:metropolitan|metro|consolidated|unified|urban)\s+(?:county\s+)?government"
    r"|urban county|city and borough|city|town|village|borough|municipality|CDP|CCD"
    r"|charter township|township|comunidad|zona urbana|plantation)$",
    re.IGNORECASE,
)
GOVERNMENT_RE = re.compile(r"government|urban county", re.IGNORECASE)


def strip_suffix(name):
    name = re.sub(r"\s*\(balance\)$", "", name.strip(), flags=re.IGNORECASE)
    return SUFFIX_RE.sub("", name, count=1)


def population_from_rows(rows):
    """``{(normalized name, ST): population}`` from Census SUB-EST rows (dicts), using the latest year.

    Names are normalised exactly as the gazetteer's are ("Miami city" -> "MIAMI"). When several places
    in a state share a name (five Miami townships in Ohio) the biggest wins.
    """
    out = {}
    for row in rows:
        if row.get("SUMLEV") not in POPULATION_SUMLEVS:
            continue
        state = places.STATES.get(row["STNAME"].upper())
        years = sorted(k for k in row if re.fullmatch(r"POPESTIMATE\d{4}", k))
        if not state or not years:
            continue
        try:
            pop = int(row[years[-1]])
        except ValueError:
            continue
        names = {places.normalize_city(strip_suffix(row["NAME"]))}
        if GOVERNMENT_RE.search(row["NAME"]):  # "Louisville/Jefferson County metro government" -> "Louisville"
            names.add(places.normalize_city(re.split(r"[-/]", strip_suffix(row["NAME"]))[0]))
        for name in names:
            out[(name, state)] = max(out.get((name, state), 0), pop)
    return out


def fetch_population():
    resp = requests.get(POPULATION_URL, headers={"User-Agent": settings.HTTP_USER_AGENT}, timeout=180)
    resp.raise_for_status()
    return population_from_rows(csv.DictReader(io.StringIO(resp.content.decode("latin-1"))))


class Command(BaseCommand):
    help = "Build the offline US places gazetteer and geocode any station cities it misses."

    def add_arguments(self, parser):
        parser.add_argument("--no-nominatim", action="store_true", help="Skip the Nominatim pass.")
        parser.add_argument(
            "--communities-only", action="store_true", help="Only rebuild us_communities.csv (GNIS)."
        )
        parser.add_argument(
            "--population-only", action="store_true",
            help="Only add Census population to the existing us_places.csv (ranks same-named places).",
        )

    def handle(self, *args, **opts):
        if opts["population_only"]:
            self.add_population()
            places._places_index.cache_clear()
            places._compact_index.cache_clear()
            places._name_index.cache_clear()
            return
        if not opts["communities_only"]:
            index = self.build_gazetteer()
            self.stdout.write(f"Wrote {len(index):,} place names to {places.PLACES_CSV.name}")
        places._places_index.cache_clear()
        places._compact_index.cache_clear()
        places._name_index.cache_clear()
        self.build_communities()
        if not opts["no_nominatim"] and not opts["communities_only"]:
            self.geocode_leftovers()

    # ------------------------------------------------------------------
    def _download(self, name):
        resp = requests.get(
            CENSUS_BASE + name, headers={"User-Agent": settings.HTTP_USER_AGENT}, timeout=120
        )
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        text = zf.read(zf.namelist()[0]).decode("utf-8")
        reader = csv.reader(io.StringIO(text), delimiter="\t")
        header = [h.strip() for h in next(reader)]
        col = {h: header.index(h) for h in ("USPS", "NAME", "ALAND", "INTPTLAT", "INTPTLONG")}
        for row in reader:
            if len(row) < len(header):
                continue
            yield (
                row[col["NAME"]].strip(),
                row[col["USPS"]].strip(),
                float(row[col["ALAND"]] or 0),
                float(row[col["INTPTLAT"]]),
                float(row[col["INTPTLONG"]]),
            )

    def build_gazetteer(self):
        self.stdout.write("Downloading Census gazetteer (places)...")
        place_rows = list(self._download(PLACES_ZIP))
        self.stdout.write("Downloading Census gazetteer (county subdivisions)...")
        cousub_rows = list(self._download(COUSUBS_ZIP))

        index = {}

        def add(name, state, lat, lon, area):
            index.setdefault((places.normalize_city(name), state), (lat, lon, area))

        # Incorporated places beat CDPs; bigger land area beats smaller.
        place_rows.sort(key=lambda r: (r[0].upper().endswith(" CDP"), -r[2]))
        # Pass order = priority: stripped names, consolidated-city aliases,
        # unstripped names ("Carson City"), then county subdivisions.
        for name, state, area, lat, lon in place_rows:
            add(strip_suffix(name), state, lat, lon, area)
        for name, state, area, lat, lon in place_rows:
            if GOVERNMENT_RE.search(name):  # "Athens-Clarke County unified government"
                head = re.split(r"[-/]", strip_suffix(name))[0]
                add(head, state, lat, lon, area)
        for name, state, area, lat, lon in place_rows:
            add(name, state, lat, lon, area)
        cousub_rows.sort(key=lambda r: -r[2])
        for name, state, area, lat, lon in cousub_rows:
            add(strip_suffix(name), state, lat, lon, area)
        # Official names that embed "City"/"Town" ("Boise City", "Amite City",
        # "Bridgewater Town") are routinely written without it ("Boise, ID").
        for name, state, area, lat, lon in place_rows + cousub_rows:
            base = strip_suffix(name)
            alias = re.sub(r"\s+(?:City|Town)$", "", base, flags=re.IGNORECASE)
            if alias != base:
                add(alias, state, lat, lon, area)

        # `pop` then `area` (land, km2) only rank same-named places when the user gives no state.
        self.stdout.write("Downloading Census population estimates...")
        population = fetch_population()
        with open(places.PLACES_CSV, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["name", "state", "lat", "lon", "area", "pop"])
            for (name, state), (lat, lon, area) in sorted(index.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                writer.writerow([name, state, f"{lat:.5f}", f"{lon:.5f}", f"{area / 1e6:.1f}", population.get((name, state), 0)])
        return index

    def add_population(self):
        """Add (or refresh) the ``pop`` column of the existing us_places.csv without rebuilding it."""
        self.stdout.write("Downloading Census population estimates...")
        population = fetch_population()
        with open(places.PLACES_CSV, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        with open(places.PLACES_CSV, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["name", "state", "lat", "lon", "area", "pop"])
            for r in rows:
                writer.writerow([r["name"], r["state"], r["lat"], r["lon"], r["area"], population.get((r["name"], r["state"]), 0)])
        known = sum(1 for r in rows if population.get((r["name"], r["state"])))
        self.stdout.write(f"Added population to {known:,} of {len(rows):,} place names in {places.PLACES_CSV.name}")

    # ------------------------------------------------------------------
    def build_communities(self):
        """USGS GNIS populated places that the Census gazetteer lacks -> us_communities.csv."""
        self.stdout.write("Downloading USGS GNIS populated places (~44 MB)...")
        resp = requests.get(GNIS_URL, headers={"User-Agent": settings.HTTP_USER_AGENT}, timeout=300)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        member = next(n for n in zf.namelist() if n.lower().endswith("domesticnames_national.txt"))

        known = set(places._places_index())  # (name, ST) already covered by Census / station overrides
        found = {}
        with zf.open(member) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""), delimiter="|")
            header = next(reader)
            col = {h: i for i, h in enumerate(header)}
            for row in reader:
                if row[col["feature_class"]] != "Populated Place":
                    continue
                state = places.STATES.get(row[col["state_name"]].upper())
                if state is None:  # territories
                    continue
                key = (places.normalize_city(row[col["feature_name"]]), state)
                if not key[0] or key in known or key in found:
                    continue
                try:
                    found[key] = (float(row[col["prim_lat_dec"]]), float(row[col["prim_long_dec"]]))
                except ValueError:
                    continue

        with open(places.COMMUNITIES_CSV, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["name", "state", "lat", "lon"])
            for (name, state), (lat, lon) in sorted(found.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                writer.writerow([name, state, f"{lat:.5f}", f"{lon:.5f}"])
        self.stdout.write(f"Wrote {len(found):,} communities to {places.COMMUNITIES_CSV.name}")

    # ------------------------------------------------------------------
    def geocode_leftovers(self):
        wanted = defaultdict(str)  # (normalized, ST) -> display name
        for row in read_fuel_csv():
            wanted[(places.normalize_city(row["city"]), row["state"])] = row["city"]
        places._compact_index.cache_clear()
        missing = sorted(k for k in wanted if places.lookup_city(wanted[k], k[1]) is None)
        self.stdout.write(
            f"{len(wanted):,} station cities; {len(missing):,} not in gazetteer -> Nominatim"
        )

        overrides = {}
        if places.OVERRIDES_JSON.exists():
            overrides = json.loads(places.OVERRIDES_JSON.read_text(encoding="utf-8"))
        failed = []
        for n, (norm, state) in enumerate(missing, 1):
            key = f"{norm}|{state}"
            if key in overrides:
                continue
            try:
                resp = requests.get(
                    settings.NOMINATIM_URL,
                    params={
                        "city": wanted[(norm, state)].title(),
                        "state": state,
                        "country": "us",
                        "format": "jsonv2",
                        "limit": 1,
                    },
                    headers={"User-Agent": settings.HTTP_USER_AGENT},
                    timeout=20,
                )
                resp.raise_for_status()
                hits = resp.json()
            except (requests.RequestException, ValueError) as exc:
                self.stderr.write(f"  {wanted[(norm, state)]}, {state}: request failed ({exc})")
                failed.append((norm, state))
                time.sleep(2)
                continue
            if hits:
                overrides[key] = [round(float(hits[0]["lat"]), 5), round(float(hits[0]["lon"]), 5)]
            else:
                failed.append((norm, state))
            if n % 25 == 0:
                self.stdout.write(f"  {n}/{len(missing)}")
            time.sleep(1.1)  # Nominatim policy: max 1 request/second

        places.OVERRIDES_JSON.write_text(
            json.dumps(dict(sorted(overrides.items())), indent=0), encoding="utf-8"
        )
        self.stdout.write(
            f"Saved {len(overrides):,} overrides; {len(failed)} cities still unresolved: {failed[:20]}"
        )
