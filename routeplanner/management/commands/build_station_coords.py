"""One-off data build: real map positions for the truck stops, from OpenStreetMap and Overture Maps.

The price file gives each stop a highway exit and a city but no coordinates, so ``load_stations``
alone places every stop at its city centre, often 5-10 miles from the real pump. This command asks
the Overpass API (once per state) for every fuel station and numbered motorway exit, adds the fuel
and convenience listings from Overture Maps (``fetch_overture_places``) as a second source of
stations, matches each stop to them (see ``services/station_match.py``), and writes

  routeplanner/data/station_coords.csv    opis_id, lat, lon, source ("site" | "uncertain" | "exit"), ref

``ref`` is the id of the matched station: ``node/123`` or ``way/45`` (OpenStreetMap), ``overture/<id>``,
or the exit's node for ``exit``. ``uncertain`` is a station match where OpenStreetMap and Overture, each
run alone, pick stations more than two miles apart. Stops it cannot match are left out of the file and keep their city
centre. Re-run ``load_stations`` afterwards. Map data (c) OpenStreetMap contributors, ODbL; places
from Overture Maps (CDLA-Permissive-2.0).

Usage:
  python manage.py build_station_coords                    # fetch (cached per state), match, write
  python manage.py build_station_coords --states AL GA     # trial run on a few states; writes nothing
  python manage.py build_station_coords --offline          # use only the cache
  python manage.py build_station_coords --offline --no-overture    # OpenStreetMap only
"""

import csv
import gzip
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routeplanner.services import places
from routeplanner.services import station_match as sm
from routeplanner.services.stations import COORDS_CSV, DATA_DIR, dedupe_by_stop, read_fuel_csv

OVERTURE_CSV = DATA_DIR / "overture_places.csv.gz"
STATE_MARGIN = 0.35  # degrees around a state's stops in which Overture places are considered

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PAUSE_SECONDS = 3  # be a polite client: one request at a time, with a pause between them
QUERIES = {
    "fuel": '[out:json][timeout:240];area["ISO3166-2"="US-{st}"]->.a;(nwr["amenity"="fuel"](area.a););out center tags;',
    # exit numbers live on the junction nodes; the highway name (I 44, US 69) lives on the way through them
    "exits": (
        '[out:json][timeout:240];area["ISO3166-2"="US-{st}"]->.a;'
        'node["highway"="motorway_junction"](area.a)->.j;'
        'way(bn.j)["highway"~"^(motorway|trunk|primary)$"]["ref"]->.w;'
        ".j out;.w out body qt;"
    ),
}


class Command(BaseCommand):
    help = "Match every truck stop to its real position using OpenStreetMap, and write station_coords.csv."

    def add_arguments(self, parser):
        parser.add_argument("--states", nargs="+", metavar="ST", help="Only these states (nothing is written).")
        parser.add_argument("--offline", action="store_true", help="Use only the cache; fetch nothing.")
        parser.add_argument(
            "--cache-dir", default=str(settings.BASE_DIR / ".osm_cache"),
            help="Where the per-state Overpass responses are kept, gzipped (default: .osm_cache/).",
        )
        parser.add_argument("--overpass-url", default=OVERPASS_URL)
        parser.add_argument("--overture", default=str(OVERTURE_CSV), help="Overture places file (from fetch_overture_places).")
        parser.add_argument("--no-overture", action="store_true", help="Use OpenStreetMap stations only.")

    def handle(self, *args, **opts):
        cache = Path(opts["cache_dir"])
        cache.mkdir(parents=True, exist_ok=True)
        overture = self._load_overture(opts)
        by_state = defaultdict(list)
        for stop in dedupe_by_stop(list(read_fuel_csv())):
            by_state[stop["state"]].append(stop)
        wanted = sorted(by_state)
        if opts["states"]:
            wanted = [s.upper() for s in opts["states"]]
            unknown = [s for s in wanted if s not in by_state]
            if unknown:
                raise CommandError(f"No stops in: {', '.join(unknown)}")

        found, tally, missing_states = [], Counter(), []
        for st in wanted:
            data = {}
            for kind in QUERIES:
                data[kind] = self._load(cache, kind, st, opts)
            if data["fuel"] is None or data["exits"] is None:
                missing_states.append(st)
                continue
            osm_sites, exit_list = sm.fuel_sites(data["fuel"]), sm.exits(data["exits"])
            ov_sites = self._overture_near(overture, by_state[st], st)
            city_of = lambda stop, st=st: places.lookup_city(stop["city"], st)
            matches = sm.locate_state(by_state[st], city_of, exit_list, osm_sites, ov_sites)
            counts = Counter()
            for stop in by_state[st]:
                if stop["opis_id"] not in matches:
                    counts["no_city"] += 1
                    continue
                loc = matches[stop["opis_id"]]
                if loc is None:
                    counts["city"] += 1
                    continue
                counts["from_overture"] += loc.source in ("site", "uncertain") and loc.osm_id.startswith("overture/")
                counts[loc.source] += 1
                found.append((stop["opis_id"], loc))
            tally.update(counts)
            self.stdout.write(
                f"{st}: {len(by_state[st])} stops, {len(osm_sites) + len(ov_sites):,} stations and {len(exit_list):,} exits on the map "
                f"-> {counts['site']} at the station, {counts['uncertain']} uncertain, {counts['exit']} at the exit, "
                f"{counts['city'] + counts['no_city']} unmatched"
            )

        total = sum(tally[k] for k in ("site", "uncertain", "exit", "city", "no_city"))
        stations = tally["site"] + tally["uncertain"]
        self.stdout.write(self.style.SUCCESS(
            f"Located {stations + tally['exit']:,} of {total:,} stops "
            f"({stations:,} at a station, of which {tally['from_overture']:,} through Overture and "
            f"{tally['uncertain']:,} uncertain because the two sources disagree; {tally['exit']:,} at the exit)."
        ))
        if missing_states:
            self.stderr.write(f"No map data for {', '.join(missing_states)} (see above); those keep city centres.")
        if opts["states"] or opts["offline"] and missing_states:
            self.stdout.write("Partial run: station_coords.csv not written.")
            return
        found.sort(key=lambda row: row[0])
        with open(COORDS_CSV, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["opis_id", "lat", "lon", "source", "ref"])
            for opis_id, loc in found:
                writer.writerow([opis_id, f"{loc.lat:.6f}", f"{loc.lon:.6f}", loc.source, loc.osm_id])
        self.stdout.write(f"Wrote {len(found):,} positions to {COORDS_CSV.name}. Now run `load_stations`.")

    # ------------------------------------------------------------------
    def _load_overture(self, opts):
        """Overture places as FuelSites (closed ones dropped), or [] if switched off or not fetched yet."""
        path = Path(opts["overture"])
        if opts["no_overture"]:
            return []
        if not path.exists():
            self.stderr.write(f"{path.name} not found: using OpenStreetMap only (run `fetch_overture_places`).")
            return []
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            sites = sm.overture_sites(csv.DictReader(fh))
        self.stdout.write(f"Overture Maps: {len(sites):,} open places loaded from {path.name}.")
        return sites

    def _overture_near(self, overture, stops, st):
        """The Overture places in the neighbourhood of one state's stops."""
        pts = [c for c in (places.lookup_city(s["city"], st) for s in stops) if c]
        if not pts or not overture:
            return []
        lat0, lat1 = min(p[0] for p in pts) - STATE_MARGIN, max(p[0] for p in pts) + STATE_MARGIN
        lon0, lon1 = min(p[1] for p in pts) - STATE_MARGIN, max(p[1] for p in pts) + STATE_MARGIN
        return [s for s in overture if lat0 <= s.lat <= lat1 and lon0 <= s.lon <= lon1]

    def _load(self, cache, kind, st, opts):
        """One state's Overpass response, from the cache or (unless --offline) the network."""
        path = cache / f"{kind}_{st}.json.gz"  # gzip: the raw JSON is ~100 MB for all states
        if path.exists() and path.stat().st_size > 50:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        if opts["offline"]:
            return None
        query = QUERIES[kind].format(st=st)
        for attempt in range(1, 6):
            reason = None
            try:
                resp = requests.post(
                    opts["overpass_url"], data={"data": query},
                    headers={"User-Agent": settings.HTTP_USER_AGENT}, timeout=300,
                )
                if resp.status_code == 200 and resp.text.lstrip().startswith("{"):
                    data = resp.json()
                    if "elements" in data:
                        with gzip.open(path, "wt", encoding="utf-8") as fh:
                            json.dump(data, fh)
                        time.sleep(PAUSE_SECONDS)
                        return data
                reason = f"HTTP {resp.status_code}"
            except (requests.RequestException, ValueError) as exc:
                reason = str(exc)[:80]
            self.stderr.write(f"  {kind} {st}: {reason}; retrying in {15 * attempt}s")
            time.sleep(15 * attempt)
        self.stderr.write(f"  {kind} {st}: gave up")
        return None
