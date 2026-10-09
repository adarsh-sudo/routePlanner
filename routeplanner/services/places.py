"""Turn user input (or CSV city/state pairs) into coordinates.

Resolution order, cheapest first:
  1. ``"lat,lon"``                      -> parsed directly           (0 network calls)
  2. ``"City, ST"`` / ``"City, State"`` -> offline Census gazetteer  (0 network calls)
  3. anything else (street address, ZIP, ...) -> one Nominatim lookup, cached
"""

import csv
import hashlib
import json
import re
import threading
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import requests
from django.conf import settings
from django.core.cache import cache

from .geo import in_conus

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLACES_CSV = DATA_DIR / "us_places.csv"
COMMUNITIES_CSV = DATA_DIR / "us_communities.csv"
OVERRIDES_JSON = DATA_DIR / "station_geocode_overrides.json"

STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA",
    "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}
STATE_ABBRS = set(STATES.values())

_CA_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
_NON_US_NAMES = {
    "CANADA", "MEXICO", "ALBERTA", "BRITISH COLUMBIA", "MANITOBA", "NEW BRUNSWICK",
    "NEWFOUNDLAND AND LABRADOR", "NOVA SCOTIA", "NORTHWEST TERRITORIES", "NUNAVUT", "ONTARIO",
    "PRINCE EDWARD ISLAND", "QUEBEC", "SASKATCHEWAN", "YUKON",
}
# Bare country names. Many are also tiny US towns (India PA, Brazil IN, Peru IL, Turkey TX), which the
# geocoder would happily match, so a country name is treated as the country. "Georgia" is a US state, so
# it is left out; "Lebanon, TN" still works because only the last part of a comma-separated input counts.
_NON_US_NAMES |= {
    "AFGHANISTAN", "ALBANIA", "ALGERIA", "ANDORRA", "ANGOLA", "ARGENTINA", "ARMENIA", "AUSTRALIA", "AUSTRIA",
    "AZERBAIJAN", "BAHAMAS", "BAHRAIN", "BANGLADESH", "BARBADOS", "BELARUS", "BELGIUM", "BELIZE", "BENIN", "BHUTAN",
    "BOLIVIA", "BOSNIA AND HERZEGOVINA", "BOTSWANA", "BRAZIL", "BRUNEI", "BULGARIA", "BURKINA FASO", "BURUNDI",
    "CAMBODIA", "CAMEROON", "CAPE VERDE", "CENTRAL AFRICAN REPUBLIC", "CHAD", "CHILE", "CHINA", "COLOMBIA",
    "COMOROS", "CONGO", "COSTA RICA", "CROATIA", "CUBA", "CYPRUS", "CZECH REPUBLIC", "CZECHIA", "DENMARK",
    "DJIBOUTI", "DOMINICA", "DOMINICAN REPUBLIC", "ECUADOR", "EGYPT", "EL SALVADOR", "ENGLAND",
    "EQUATORIAL GUINEA", "ERITREA", "ESTONIA", "ESWATINI", "ETHIOPIA", "FIJI", "FINLAND", "FRANCE", "GABON",
    "GAMBIA", "GERMANY", "GHANA", "GREECE", "GRENADA", "GUATEMALA", "GUINEA", "GUYANA", "HAITI", "HONDURAS",
    "HUNGARY", "ICELAND", "INDIA", "INDONESIA", "IRAN", "IRAQ", "IRELAND", "ISRAEL", "ITALY", "IVORY COAST",
    "JAMAICA", "JAPAN", "JORDAN", "KAZAKHSTAN", "KENYA", "KIRIBATI", "KOSOVO", "KUWAIT", "KYRGYZSTAN", "LAOS",
    "LATVIA", "LEBANON", "LESOTHO", "LIBERIA", "LIBYA", "LIECHTENSTEIN", "LITHUANIA", "LUXEMBOURG", "MADAGASCAR",
    "MALAWI", "MALAYSIA", "MALDIVES", "MALI", "MALTA", "MARSHALL ISLANDS", "MAURITANIA", "MAURITIUS",
    "MICRONESIA", "MOLDOVA", "MONACO", "MONGOLIA", "MONTENEGRO", "MOROCCO", "MOZAMBIQUE", "MYANMAR", "NAMIBIA",
    "NAURU", "NEPAL", "NETHERLANDS", "NEW ZEALAND", "NICARAGUA", "NIGER", "NIGERIA", "NORTH KOREA",
    "NORTH MACEDONIA", "NORWAY", "OMAN", "PAKISTAN", "PALAU", "PALESTINE", "PANAMA", "PAPUA NEW GUINEA",
    "PARAGUAY", "PERU", "PHILIPPINES", "POLAND", "PORTUGAL", "QATAR", "ROMANIA", "RUSSIA", "RWANDA", "SAMOA",
    "SAN MARINO", "SAUDI ARABIA", "SCOTLAND", "SENEGAL", "SERBIA", "SEYCHELLES", "SIERRA LEONE", "SINGAPORE",
    "SLOVAKIA", "SLOVENIA", "SOLOMON ISLANDS", "SOMALIA", "SOUTH AFRICA", "SOUTH KOREA", "SOUTH SUDAN", "SPAIN",
    "SRI LANKA", "SUDAN", "SURINAME", "SWEDEN", "SWITZERLAND", "SYRIA", "TAIWAN", "TAJIKISTAN", "TANZANIA",
    "THAILAND", "TOGO", "TONGA", "TRINIDAD AND TOBAGO", "TUNISIA", "TURKEY", "TURKMENISTAN", "TUVALU", "UGANDA",
    "UKRAINE", "UNITED ARAB EMIRATES", "UNITED KINGDOM", "UK", "URUGUAY", "UZBEKISTAN", "VANUATU",
    "VATICAN CITY", "VENEZUELA", "VIETNAM", "WALES", "YEMEN", "ZAMBIA", "ZIMBABWE",
}
_WORD_MAP = {"SAINT": "ST", "MOUNT": "MT", "FORT": "FT", "SAINTE": "STE", "PORT": "PT"}
_COORD_RE = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


class LocationError(ValueError):
    """The input could not be resolved to a usable US location (HTTP 400)."""


class GeocodingUnavailable(RuntimeError):
    """A needed external geocoder failed (HTTP 502)."""


@dataclass(frozen=True)
class Place:
    query: str
    label: str
    lat: float
    lon: float
    source: str  # "coordinates" | "gazetteer" | "nominatim"
    external_calls: int = 0  # network calls actually made (0 on a cache hit)
    resolved_address: str = ""  # the geocoder's full match, or a note when a bare name was ambiguous
    alternatives: tuple = ()  # other US places with the same bare name, e.g. ("India, PA", ...)


def normalize_city(name):
    """Canonical form for matching: upper-case, punctuation-free, ST/MT/FT style."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")  # Cañon -> Canon
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[.'`]", "", s)
    s = re.sub(r"[-/,()]", " ", s)
    tokens = [_WORD_MAP.get(t, t) for t in s.split()]
    return " ".join(tokens)


@lru_cache(maxsize=1)
def _places_index():
    """dict[(normalized_city, ST)] -> (lat, lon), from the committed gazetteer."""
    index = {}
    if PLACES_CSV.exists():
        with open(PLACES_CSV, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                index[(row["name"], row["state"])] = (float(row["lat"]), float(row["lon"]))
    if OVERRIDES_JSON.exists():
        for key, (lat, lon) in json.loads(OVERRIDES_JSON.read_text(encoding="utf-8")).items():
            city, _, state = key.rpartition("|")
            index.setdefault((city, state), (lat, lon))
    return index


@lru_cache(maxsize=1)
def _compact_index():
    """Same data keyed without spaces, so "De Forest" finds "DeForest"."""
    compact = {}
    for (name, state), coords in _places_index().items():
        compact.setdefault((name.replace(" ", ""), state), coords)
    return compact


def lookup_city(city, state):
    """Offline lookup of a city/state pair -> (lat, lon) or None."""
    key = (normalize_city(city), state.upper())
    found = _places_index().get(key)
    if found is None:
        found = _compact_index().get((key[0].replace(" ", ""), key[1]))
    return found


@lru_cache(maxsize=1)
def _name_index():
    """dict[normalized_name] -> [(tier, -pop, -area, ST, lat, lon), ...], best first.

    Every named US settlement we know offline. Tier 0 is the Census gazetteer (incorporated
    places, CDPs, townships) plus geocoded station cities; tier 1 is the GNIS communities
    (hamlets and other unincorporated places). Within a tier the more populous place wins (Miami,
    FL over the Miami township in Kansas), then the bigger land area (Census does not estimate
    CDPs, so they come after places that have a population); GNIS has neither, so same-named
    communities are ordered by state.
    """
    by_name = defaultdict(list)
    seen = set()

    def add(name, state, lat, lon, tier, area=0.0, pop=0):
        seen.add((name, state))
        by_name[name].append((tier, -pop, -area, state, lat, lon))

    if PLACES_CSV.exists():
        with open(PLACES_CSV, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                add(row["name"], row["state"], float(row["lat"]), float(row["lon"]), 0,
                    float(row.get("area") or 0), int(row.get("pop") or 0))
    if OVERRIDES_JSON.exists():
        for key, (lat, lon) in json.loads(OVERRIDES_JSON.read_text(encoding="utf-8")).items():
            city, _, state = key.rpartition("|")
            if (city, state) not in seen:
                add(city, state, lat, lon, 0)
    if COMMUNITIES_CSV.exists():
        with open(COMMUNITIES_CSV, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if (row["name"], row["state"]) not in seen:
                    add(row["name"], row["state"], float(row["lat"]), float(row["lon"]), 1)
    for entries in by_name.values():
        entries.sort()
    return dict(by_name)


def lookup_community(city, state):
    """Offline lookup of an unincorporated community (GNIS) -> (lat, lon) or None."""
    state = state.upper()
    for tier, _, _, st, lat, lon in _name_index().get(normalize_city(city), ()):
        if st == state:
            return lat, lon
    return None


def lookup_name(name):
    """Every known US place called ``name``, best first: [(ST, lat, lon), ...]."""
    return [(st, lat, lon) for _, _, _, st, lat, lon in _name_index().get(normalize_city(name), ())]


def _split_city_state(text):
    """'Austin, TX' / 'Austin, Texas' / 'Austin TX' -> ('Austin', 'TX') or None."""
    if re.search(r"\d", text):  # street address / ZIP: leave to the geocoder
        return None
    text = text.strip().strip(",")
    if "," in text:
        parts = [p.strip() for p in text.split(",")]
        city, state = parts[0], parts[1]
        if len(parts) > 2 and parts[2].upper() in ("USA", "US", "UNITED STATES"):
            pass  # "Austin, TX, USA"
        elif len(parts) > 2:
            return None
    else:
        tokens = text.split()
        if len(tokens) < 2:
            return None
        # try a 2-word then 1-word trailing state name / abbreviation
        for n in (2, 1):
            tail = " ".join(tokens[-n:]).upper()
            if tail in STATES or (n == 1 and tail in STATE_ABBRS):
                city, state = " ".join(tokens[:-n]), tail
                break
        else:
            return None
    state = state.upper().strip(".")
    state = STATES.get(state, state)
    if state not in STATE_ABBRS or not city:
        return None
    return city, state


def _looks_non_us(text):
    """True if the text qualifies a place with a Canadian province or a non-US country.

    The geocoder is restricted to the US, so without this "Calgary, AB" would
    fuzzy-match a tiny Calgary in Texas and silently route from the wrong place.
    A lone country name is NOT rejected here: "India" and "Mexico" are also US towns,
    so a bare name goes to the US gazetteer first (see resolve_place).
    """
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return False
    last = parts[-1].upper()
    if len(parts) > 1 and (last in _NON_US_NAMES or last in _CA_PROVINCES):
        return True
    tokens = text.split()
    # "Vancouver BC": only an upper-case trailing abbreviation counts.
    return len(parts) == 1 and len(tokens) > 1 and tokens[-1] in _CA_PROVINCES


NOMINATIM_MIN_INTERVAL = 1.0  # seconds; Nominatim's usage policy is max 1 request/second
MIN_PLACE_IMPORTANCE = 0.3  # Nominatim "importance" (0-1) a free-text match needs to be believed
_nominatim_lock = threading.Lock()
_nominatim_last_call = 0.0


def _throttle_nominatim():
    global _nominatim_last_call
    wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _nominatim_last_call)
    if wait > 0:
        time.sleep(wait)
    _nominatim_last_call = time.monotonic()


def _nominatim(query, require_known=False):
    """Return ((lat, lon, display_name), network_calls_made).

    ``require_known`` rejects a weak match (see MIN_PLACE_IMPORTANCE), for inputs that
    carry no state or street number to anchor them.
    """
    # Hash the query: raw user text is not a safe cache key (spaces, length).
    cache_key = "nominatim:" + hashlib.sha1(query.strip().lower().encode("utf-8")).hexdigest()
    hit = cache.get(cache_key)
    if hit is not None:
        return hit, 0
    if not settings.NOMINATIM_ENABLED:
        raise LocationError(
            f"Could not resolve {query!r} offline. Use 'lat,lon' or 'City, ST' "
            "(address geocoding is disabled)."
        )
    try:
        with _nominatim_lock:
            _throttle_nominatim()
            resp = requests.get(
                settings.NOMINATIM_URL,
                params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
                headers={"User-Agent": settings.HTTP_USER_AGENT},
                timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            results = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise GeocodingUnavailable(f"Geocoding service failed for {query!r}: {exc}") from exc
    if not results:
        raise LocationError(f"No US location found for {query!r}.")
    top = results[0]
    # Without a state or street number the geocoder fuzzy-matches anything: "xyz" is a trail in
    # Georgia (importance 0.04), "India" a hamlet in Pennsylvania (0.13). Chicago is 0.80 and
    # Mount Rushmore 0.55. A missing score is let through rather than rejecting everything.
    if require_known and float(top.get("importance", 1.0)) < MIN_PLACE_IMPORTANCE:
        raise LocationError(
            f"{query!r} doesn't match a well-known US place. "
            "Try 'City, ST', a street address, or 'lat,lon'."
        )
    value = (float(top["lat"]), float(top["lon"]), top.get("display_name", query))
    cache.set(cache_key, value, 60 * 60 * 24 * 7)
    return value, 1


def _bare_name_place(text, matches):
    """A Place for a name given without a state ("India"), from the offline US gazetteer.

    ``matches`` is lookup_name(text), best first. When several US places share the name the
    best one is used and the others are reported, so the caller can see the guess and fix it
    by adding a state.
    """
    name = text.strip().title()
    state, lat, lon = matches[0]
    label = f"{name}, {state}"
    others = list(dict.fromkeys(st for st, _, _ in matches[1:] if st != state))
    alternatives = tuple(f"{name}, {st}" for st in others[:5])
    note = ""
    if len(matches) > 1:
        note = (
            f"{label} is one of {len(matches)} US places named {name}; add a state to choose another"
            + (f" (also: {'; '.join(alternatives)})" if alternatives else "")
            + "."
        )
    return Place(text, label, lat, lon, "gazetteer", 0, note, alternatives)


def resolve_place(text):
    """Resolve user input to a Place. Raises LocationError / GeocodingUnavailable."""
    if not isinstance(text, str) or not text.strip():
        raise LocationError("Location is required.")
    text = text.strip()

    m = _COORD_RE.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise LocationError(f"Coordinates out of range: {text!r}.")
        place = Place(text, f"{lat:.4f}, {lon:.4f}", lat, lon, "coordinates")
    else:
        if _looks_non_us(text):
            raise LocationError(f"{text!r} is not in the USA; only US locations are supported.")
        split = _split_city_state(text)
        found = (lookup_city(*split) or lookup_community(*split)) if split else None
        if found:
            city, state = split
            place = Place(text, f"{city.title()}, {state}", found[0], found[1], "gazetteer")
        else:
            bare = split is None and "," not in text and re.search(r"\d", text) is None
            matches = lookup_name(text) if bare else []
            if matches:
                place = _bare_name_place(text, matches)
            elif bare and text.upper() in _NON_US_NAMES:
                raise LocationError(
                    f"{text!r} is not in the USA; only US locations are supported. "
                    "For a US town, add the state, e.g. 'Austin, TX'."
                )
            else:
                # A street number / ZIP or a "City, ST" form anchors the lookup; a bare name does not.
                (lat, lon, display), calls = _nominatim(text, require_known=bare)
                place = Place(text, text, lat, lon, "nominatim", calls, display)

    if not in_conus(place.lat, place.lon):
        raise LocationError(
            f"{text!r} resolves outside the contiguous USA "
            f"({place.lat:.3f}, {place.lon:.3f}); only drivable US routes are supported."
        )
    return place
