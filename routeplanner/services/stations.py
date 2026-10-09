"""Station data: CSV parsing (for loading) and the in-memory index (for requests)."""

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FUEL_CSV = DATA_DIR / "fuel-prices.csv"
COORDS_CSV = DATA_DIR / "station_coords.csv"  # real positions, written by build_station_coords

# The supplied file mixes in Canadian truck stops; the exercise is USA-only.
NON_US = {"ON", "AB", "BC", "MB", "SK", "YT", "QC", "NS", "NB", "NL", "PE", "NT", "NU"}


def read_fuel_csv(path=FUEL_CSV):
    """Yield cleaned US rows from the raw price file (one dict per CSV row)."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            state = raw["State"].strip().upper()
            if state in NON_US or not raw["Retail Price"].strip():
                continue
            yield {
                "opis_id": int(raw["OPIS Truckstop ID"]),
                "name": re.sub(r"\s+", " ", raw["Truckstop Name"]).strip(),
                "address": re.sub(r"\s+", " ", raw["Address"]).strip(),
                "city": re.sub(r"\s+", " ", raw["City"]).strip(),
                "state": state,
                "price": float(raw["Retail Price"]),
            }


def read_station_coords(path=COORDS_CSV):
    """``{opis_id: (lat, lon, source)}`` for the stops with a real position; {} if the file is absent."""
    if not Path(path).exists():
        return {}
    with open(path, encoding="utf-8", newline="") as fh:
        return {
            int(r["opis_id"]): (float(r["lat"]), float(r["lon"]), r["source"])
            for r in csv.DictReader(fh)
        }


def dedupe_by_stop(rows):
    """Collapse repeated rows for one OPIS truck stop into a single record.

    A stop can appear several times (different name spellings, several prices).
    We keep the longest name and the LOWEST listed price - the cheapest fuel a
    driver can actually buy there. Change ``min`` to adjust the policy.
    """
    stops = {}
    for row in rows:
        cur = stops.get(row["opis_id"])
        if cur is None:
            stops[row["opis_id"]] = dict(row)
            continue
        cur["price"] = min(cur["price"], row["price"])
        if len(row["name"]) > len(cur["name"]):
            cur["name"] = row["name"]
    return list(stops.values())


@dataclass(frozen=True, slots=True)
class StationRec:
    id: int
    name: str
    address: str
    city: str
    state: str
    lat: float
    lon: float
    price: float
    location_source: str = "city"  # "site" | "exit" | "city": how precisely lat/lon is known


class StationIndex:
    """All stations in memory (a few thousand rows) plus a bounding-box prefilter."""

    def __init__(self, stations):
        self.stations = stations

    def in_bbox(self, lat_min, lat_max, lon_min, lon_max):
        return [
            s for s in self.stations
            if lat_min <= s.lat <= lat_max and lon_min <= s.lon <= lon_max
        ]


class StationsNotLoaded(RuntimeError):
    """The Station table is empty (HTTP 503)."""


@lru_cache(maxsize=1)
def get_station_index():
    """Load stations from the DB once per process (failures are not cached)."""
    from routeplanner.models import Station

    stations = [
        StationRec(
            r.opis_id, r.name, r.address, r.city, r.state, r.latitude, r.longitude, r.price, r.location_source
        )
        for r in Station.objects.all().iterator()
    ]
    if not stations:
        raise StationsNotLoaded(
            "No fuel stations are loaded. Run `python manage.py load_stations` and retry."
        )
    return StationIndex(stations)
