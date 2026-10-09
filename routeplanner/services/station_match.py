"""Find where a truck stop really is, from open map data. Pure functions: no Django, no network.

The price file has no coordinates. Each row gives a highway exit ("I-44, EXIT 283 & US-69"), a name
("PILOT TRAVEL CENTER #1243") and a city. Street geocoders cannot read that, but OpenStreetMap knows
every numbered exit and most fuel stations, and Overture Maps' business listings fill many of the
stations OpenStreetMap lacks (store numbers included), so we match in tiers, best first:

  site  a fuel station (from either source) whose name fits the stop and that sits at the stop's exit
        (or, when the row has no exit number, in its city)
  uncertain  matched like a site, but the two sources, each run alone, pick stations more than two
        miles apart (several stores of one chain, and the price file cannot say which is meant)
  exit  the OSM motorway junction for that highway + exit number; the stop is at the exit, so this is
        close, though not the pump itself
  (none) the caller falls back to the city centre, as before

``build_station_coords`` loads the map data and calls ``locate`` for every stop.
"""

import dataclasses
import math
import re
from dataclasses import dataclass

from .geo import haversine_miles

EXIT_RE = re.compile(r"\bEXITS?\s*([0-9]+[A-Z]?)\b", re.IGNORECASE)
ROUTE_RE = re.compile(r"\b(I|US)\s*-\s*(\d+)\b", re.IGNORECASE)
OSM_ROUTE_RE = re.compile(r"^\s*(I|US)\s*-?\s*(\d+)\b", re.IGNORECASE)

SITE_RADIUS_AT_EXIT = 2.5  # miles from the junction in which a named fuel station counts as "at the exit"
# With no exit number the city centre is all we know. A same-brand station in the next town is worse
# than the centre itself, so only a close match is believed. Measured against stops whose store
# number is on the map: found within ~8 miles of the centre, the match is far nearer the truth than
# the centre is (median 0.0 mi vs 1.8-6.6 mi); at 8-10 miles it is no better; beyond that, worse.
SITE_RADIUS_IN_CITY = 8.0
MAX_EXIT_FROM_CITY = 25.0  # an exit farther than this from the stated city is a data error in the row
MIN_NAME_SCORE = 0.5

# Words that say "this is a fuel stop" and so tell stops apart from nothing.
GENERIC = frozenset(
    "travel center centers centre stop stops stopping truck trucks plaza country store stores fuel fuels "
    "gas station stations mart food shop express auto service services the of and inc llc co company oil "
    "petroleum convenience market marketplace mini super corp corporation".split()
)
ALIASES = {"travelcenters": "ta", "love": "loves"}


def parse_address(address):
    """``'I-44, EXIT 283 & US-69'`` -> ``({'I 44', 'US 69'}, '283')``; the exit is None if absent."""
    routes = {f"{kind.upper()} {num}" for kind, num in ROUTE_RE.findall(address or "")}
    m = EXIT_RE.search(address or "")
    return routes, (m.group(1).upper() if m else None)


def route_set(ref):
    """OSM way ``ref`` such as ``'I 44;US 69;OK 3'`` -> ``{'I 44', 'US 69'}`` (state routes are ignored)."""
    out = set()
    for part in (ref or "").split(";"):
        m = OSM_ROUTE_RE.match(part)
        if m:
            out.add(f"{m.group(1).upper()} {m.group(2)}")
    return out


def exit_matches(wanted, osm_ref):
    """True if an OSM exit ``ref`` ('283', '283A', '283A-B', '283;284') covers the wanted exit number."""
    for tok in re.split(r"[;,/\s]+", (osm_ref or "").upper()):
        if tok == wanted or (wanted.isdigit() and re.fullmatch(rf"{wanted}[A-Z](-[A-Z])?", tok)):
            return True
    return False


def name_tokens(*names):
    """Distinctive lower-case words of one or more names, without apostrophes and filler words."""
    words = re.sub(r"['’`]", "", " ".join(n for n in names if n).lower())
    toks = {ALIASES.get(w, w) for w in re.findall(r"[a-z0-9]+", words)}
    return frozenset(t for t in toks if t not in GENERIC and not t.isdigit())


def store_numbers(*texts):
    """Store numbers (2+ digits) found in the given strings, e.g. 'PILOT #1243' -> {'1243'}."""
    return frozenset(n for t in texts if t for n in re.findall(r"(?<![\d.])\d{2,}(?![\d.])", t))


@dataclass(frozen=True, slots=True)
class FuelSite:
    osm_id: str
    lat: float
    lon: float
    tokens: frozenset
    numbers: frozenset
    truck_friendly: bool


@dataclass(frozen=True, slots=True)
class Located:
    lat: float
    lon: float
    source: str  # "site" or "exit" ("uncertain" is set by locate_state)
    osm_id: str


def fuel_sites(overpass):
    """FuelSites from an Overpass ``out center tags`` response for ``amenity=fuel``."""
    sites = []
    for el in overpass.get("elements", []):
        tags = el.get("tags") or {}
        point = el if "lat" in el else el.get("center")
        if not point:
            continue
        label = (tags.get("name"), tags.get("brand"), tags.get("operator"))
        sites.append(
            FuelSite(
                osm_id=f"{el['type']}/{el['id']}",
                lat=point["lat"],
                lon=point["lon"],
                tokens=name_tokens(*label),
                numbers=store_numbers(tags.get("name"), tags.get("ref"), tags.get("branch"), tags.get("ref:store")),
                truck_friendly=tags.get("hgv") == "yes" or tags.get("fuel:HGV_diesel") == "yes",
            )
        )
    return sites


@dataclass(frozen=True, slots=True)
class Exit:
    osm_id: str
    lat: float
    lon: float
    ref: str
    routes: frozenset


def exits(overpass):
    """Exits from an Overpass response holding junction nodes and the numbered ways through them."""
    elements = overpass.get("elements", [])
    routes_at = {}  # node id -> routes of the highways through it
    for el in elements:
        if el["type"] == "way":
            routes = route_set((el.get("tags") or {}).get("ref"))
            if routes:
                for node_id in el.get("nodes", ()):
                    routes_at.setdefault(node_id, set()).update(routes)
    found = []
    for el in elements:
        ref = (el.get("tags") or {}).get("ref")
        if el["type"] == "node" and ref and el["id"] in routes_at:
            found.append(Exit(f"node/{el['id']}", el["lat"], el["lon"], ref, frozenset(routes_at[el["id"]])))
    return found


def find_exit(exit_list, routes, exit_no, near):
    """The exit on one of ``routes`` numbered ``exit_no`` that lies closest to ``near`` (lat, lon)."""
    best = None
    for ex in exit_list:
        if ex.routes & routes and exit_matches(exit_no, ex.ref):
            d = haversine_miles(near[0], near[1], ex.lat, ex.lon)
            if d <= MAX_EXIT_FROM_CITY and (best is None or d < best[0]):
                best = (d, ex)
    return best[1] if best else None


def name_score(stop_tokens, stop_numbers, site):
    """0 = no shared distinctive word; 0.5-1 = words agree; +1 when the store number agrees too."""
    shared = stop_tokens & site.tokens
    if not shared:
        return 0.0
    score = len(shared) / len(stop_tokens)
    if stop_numbers & site.numbers:
        score += 1.0
    return score + (0.05 if site.truck_friendly else 0.0)


class SiteIndex:
    """Fuel stations bucketed in a grid, so a search near one point does not scan a whole state."""

    CELL = 0.1  # degrees, about 7 miles

    def __init__(self, sites):
        self.sites = list(sites)
        self._cells = {}
        for s in self.sites:
            self._cells.setdefault(self._key(s.lat, s.lon), []).append(s)

    def _key(self, lat, lon):
        return int(lat // self.CELL), int(lon // self.CELL)

    def near(self, lat, lon, radius_miles):
        """Sites in the grid cells that cover a circle of ``radius_miles`` (a superset of those inside it)."""
        d_lat = radius_miles / 69.0
        d_lon = d_lat / max(0.2, math.cos(math.radians(lat)))
        (i0, j0), (i1, j1) = self._key(lat - d_lat, lon - d_lon), self._key(lat + d_lat, lon + d_lon)
        return [s for i in range(i0, i1 + 1) for j in range(j0, j1 + 1) for s in self._cells.get((i, j), ())]

    def __len__(self):
        return len(self.sites)

    def __iter__(self):
        return iter(self.sites)


def merge_sites(sites, radius_miles=0.15):
    """One FuelSite per physical station, when two lists describe the same one.

    A site within ``radius_miles`` of an earlier one that shares a distinctive word is folded into it:
    the earlier position and id win, and the words and store numbers are combined (so a name from one
    source and a "#674" from the other end up on one station). Put the preferred source first.
    """
    out, cells = [], {}
    for s in sites:
        i, j = int(s.lat // SiteIndex.CELL), int(s.lon // SiteIndex.CELL)
        twin = next(
            (k for di in (-1, 0, 1) for dj in (-1, 0, 1) for k in cells.get((i + di, j + dj), ())
             if out[k].tokens & s.tokens and haversine_miles(out[k].lat, out[k].lon, s.lat, s.lon) <= radius_miles),
            None,
        )
        if twin is None:
            cells.setdefault((i, j), []).append(len(out))
            out.append(s)
        else:
            t = out[twin]
            out[twin] = dataclasses.replace(
                t, tokens=t.tokens | s.tokens, numbers=t.numbers | s.numbers,
                truck_friendly=t.truck_friendly or s.truck_friendly,
            )
    return out


OVERTURE_PENALTY_MILES = 1.5  # see best_site
OVERTURE_CLOSED = frozenset({"permanently_closed", "temporarily_closed"})
OVERTURE_TRUCK_TAXONOMY = frozenset({"truck_stop", "truck_gas_station"})


def overture_sites(rows):
    """FuelSites from Overture Maps place rows (dicts with id, name, lat, lon, status, tax); closed ones are dropped."""
    sites = []
    for r in rows:
        if (r.get("status") or "") in OVERTURE_CLOSED or not r.get("name"):
            continue
        sites.append(
            FuelSite(
                osm_id=f"overture/{r['id']}",
                lat=float(r["lat"]),
                lon=float(r["lon"]),
                tokens=name_tokens(r["name"]),
                numbers=store_numbers(r["name"]),
                truck_friendly=(r.get("tax") or "") in OVERTURE_TRUCK_TAXONOMY,
            )
        )
    return sites


def best_site(sites, name, around, radius, exclude=()):
    """The best-named fuel station within ``radius`` miles of ``around`` (lat, lon), or None.

    ``sites`` is a list or a ``SiteIndex``. ``exclude`` holds station ids already given to another stop.
    """
    stop_tokens = name_tokens(name)
    if not stop_tokens:
        return None
    stop_numbers = store_numbers(name)
    # About 0.0145 degrees of latitude per mile; the prefilter keeps this cheap on a state's 10k sites.
    box = radius / 55.0
    best = None
    for site in (sites.near(around[0], around[1], radius) if isinstance(sites, SiteIndex) else sites):
        if site.osm_id in exclude:
            continue
        if abs(site.lat - around[0]) > box or abs(site.lon - around[1]) > box * 1.6:
            continue
        score = name_score(stop_tokens, stop_numbers, site)
        if score < MIN_NAME_SCORE:
            continue
        d = haversine_miles(around[0], around[1], site.lat, site.lon)
        if d > radius:
            continue
        # Between equally named stations the nearer wins, but an Overture listing has to be clearly nearer
        # than an OpenStreetMap one: OSM is the better-placed source, and a store number overrides this anyway.
        key = (-score, d + (OVERTURE_PENALTY_MILES if site.osm_id.startswith("overture/") else 0.0))
        if best is None or key < best[0]:
            best = (key, site)
    return best[1] if best else None


def has_exit(address):
    """True if the address names a highway and an exit number, so ``locate`` can use the exit."""
    routes, exit_no = parse_address(address)
    return bool(routes and exit_no)


def locate(name, address, city_xy, exit_list, sites, claimed=frozenset()):
    """Best known position of one truck stop, as ``Located``, or None to keep the city centre.

    ``claimed`` holds OSM stations already given to other stops. It only restricts the no-exit
    fallback, where several same-brand stores in one city would otherwise all pick the same
    nearest pump; at an exit the exit number already pins the stop down.
    """
    routes, exit_no = parse_address(address)
    if routes and exit_no:
        ex = find_exit(exit_list, routes, exit_no, city_xy)
        if ex:
            site = best_site(sites, name, (ex.lat, ex.lon), SITE_RADIUS_AT_EXIT)
            if site:
                return Located(site.lat, site.lon, "site", site.osm_id)
            return Located(ex.lat, ex.lon, "exit", ex.osm_id)
    site = best_site(sites, name, city_xy, SITE_RADIUS_IN_CITY, exclude=claimed)
    if site:
        return Located(site.lat, site.lon, "site", site.osm_id)
    return None


DISAGREE_MILES = 2.0  # two sources that pick stations farther apart than this disagree


def match_stops(stops, city_of, exit_list, sites):
    """Locate every stop of one state against one list of stations: ``{opis_id: Located or None}``.

    Stops with an exit number go first (their match is the more certain one, and it keeps those
    stations out of reach of the no-exit stops, which are matched by city and name only), and each
    station is given to one stop only. A stop whose city is unknown (``city_of`` returns None) is
    left out of the result.
    """
    out, claimed = {}, set()
    for stop in sorted(stops, key=lambda s: not has_exit(s["address"])):
        city = city_of(stop)
        if city is None:
            continue
        loc = locate(stop["name"], stop["address"], city, exit_list, sites, claimed)
        if loc and loc.source == "site":
            claimed.add(loc.osm_id)
        out[stop["opis_id"]] = loc
    return out


def disagree(a, b, miles=DISAGREE_MILES):
    """True if both found a station for the stop and the two stations are more than ``miles`` apart."""
    return bool(
        a and b and a.source == b.source == "site"
        and haversine_miles(a.lat, a.lon, b.lat, b.lon) > miles
    )


def locate_state(stops, city_of, exit_list, osm_sites, overture_sites):
    """``match_stops`` over both sources together, with ``uncertain`` marking where they disagree.

    The result is the merged match (OpenStreetMap preferred). Each source is also run alone; where
    the two pick stations more than ``DISAGREE_MILES`` apart, the stop is matched to one of two
    different stores of the same chain and we cannot tell which, so its ``source`` becomes
    ``"uncertain"`` (the position stays the merged pick). One source finding nothing is not a
    disagreement, and with no Overture sites nothing is flagged.
    """
    final = match_stops(stops, city_of, exit_list, SiteIndex(merge_sites(osm_sites + overture_sites)))
    if not overture_sites:
        return final
    from_osm = match_stops(stops, city_of, exit_list, SiteIndex(merge_sites(osm_sites)))
    from_overture = match_stops(stops, city_of, exit_list, SiteIndex(merge_sites(overture_sites)))
    for opis_id, loc in final.items():
        if loc and loc.source == "site" and disagree(from_osm.get(opis_id), from_overture.get(opis_id)):
            final[opis_id] = dataclasses.replace(loc, source="uncertain")
    return final
