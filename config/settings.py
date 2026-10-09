"""
Settings for the fuel-optimized route API.

Deliberately lean: this is a stateless JSON API, so no admin/auth/sessions/CSRF.
Tunables for the planner live at the bottom and can be overridden via env vars.
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .envfile import load_for_settings

BASE_DIR = Path(__file__).resolve().parent.parent

# Settings can live in a private .env file next to manage.py (copy .env.example). A real environment variable
# always wins over the file. DOTENV_PATH points somewhere else. `manage.py test` ignores the default .env.
load_for_settings(BASE_DIR / ".env", sys.argv)


def _env_float(name, default):
    return float(os.environ.get(name, default))


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "django-insecure-dev-only-change-me-in-production"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")

INSTALLED_APPS = [
    "routeplanner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",  # route geometry compresses ~5x
    "django.middleware.common.CommonMiddleware",
]

# Django's default ("same-origin") sends no Referer to tile.openstreetmap.org, which
# answers 403 "Access blocked" to tile requests without one.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Process-local cache: memoises routes and geocodes so repeat requests are ~ms.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuelroute",
        "TIMEOUT": 60 * 60 * 24,
        "OPTIONS": {"MAX_ENTRIES": 2000},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
STATIC_URL = "static/"

# ---------------------------------------------------------------------------
# Route planner configuration
# ---------------------------------------------------------------------------
FUEL_PLANNER = {
    # Vehicle
    "RANGE_MILES": _env_float("FUEL_RANGE_MILES", 500),
    "MPG": _env_float("FUEL_MPG", 10),
    # Stations are only city-centroid accurate (the CSV has no coordinates), so
    # we accept anything within this many miles of the route.
    "SEARCH_RADIUS_MILES": _env_float("FUEL_SEARCH_RADIUS_MILES", 10),
    # Dollar value of the time/hassle of one fuel stop. A stop is only made if it
    # saves more than this in fuel, which prevents chains of micro-stops that chase
    # fractions of a cent. Set to 0 for the pure cheapest-fuel plan.
    "STOP_PENALTY_USD": _env_float("FUEL_STOP_PENALTY_USD", 10),
    # The starting tank is priced at the cheapest station within this radius of
    # the origin (falls back to the nearest station).
    "START_PRICE_RADIUS_MILES": _env_float("FUEL_START_PRICE_RADIUS_MILES", 50),
    # Route polyline is thinned to roughly this spacing for matching + output.
    "ROUTE_SPACING_MILES": _env_float("FUEL_ROUTE_SPACING_MILES", 0.5),
    # After planning, measure the real driven detour to the chosen stations (and their likely
    # stand-ins) with ONE extra routing call, then plan again. Off = straight-line detours only.
    "DRIVEN_DETOURS": os.environ.get("FUEL_DRIVEN_DETOURS", "1") == "1",
    # That call is skipped if the request has already made this many external calls (geocoding
    # plus routing), so two street-address inputs stay within the 3-call ceiling.
    "MAX_EXTERNAL_CALLS": int(_env_float("FUEL_MAX_EXTERNAL_CALLS", 3)),
    # A and B for the detour measurement are the route points this far before / after the station.
    "DETOUR_SPAN_MILES": _env_float("FUEL_DETOUR_SPAN_MILES", 10),
    # Stations measured per call (3 coordinates each; the public server accepts at most 100): the
    # chosen stops first, then their likeliest stand-ins. 10 gives the same stops and a cost within
    # $0.07 of 30 on 12 test routes, and the call takes ~0.3 s instead of ~0.7 s.
    "DETOUR_CHECK_LIMIT": int(_env_float("FUEL_DETOUR_CHECK_LIMIT", 10)),
}

# OSRM (free, keyless, plans CAR routes: only a fallback for when there is no key). Point OSRM_BASE_URL at a self-hosted instance for heavy use.
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_TIMEOUT_SECONDS = _env_float("OSRM_TIMEOUT_SECONDS", 20)
# The public server asks for at most 1 request per second. 0 disables the pause.
OSRM_MIN_INTERVAL_SECONDS = _env_float("OSRM_MIN_INTERVAL_SECONDS", 1.0)

# OpenRouteService. The key is sent in a request header and stays on the server; never put it in the
# page, a URL or a log. Free plan: 2,000 route and 500 matrix requests a day, 40 a minute, routes up to
# 6,000 km (a search makes one route request and one matrix request).
ORS_BASE_URL = os.environ.get("ORS_BASE_URL", "https://api.openrouteservice.org").rstrip("/")
ORS_API_KEY = os.environ.get("ORS_API_KEY", "").strip()

# Routing engine: "ors" plans TRUCK routes (OpenRouteService, driving-hgv profile; free key from
# https://openrouteservice.org/dev/#/signup) and "osrm" plans CAR routes. With a key the default is "ors", so a key
# is all it takes; without one the app still runs on "osrm" and the page says the route is for a car.
# ROUTING_ENGINE=osrm forces car routes even when there is a key. A blank value counts as not set.
ROUTING_ENGINE = (os.environ.get("ROUTING_ENGINE") or ("ors" if ORS_API_KEY else "osrm")).strip().lower()

ORS_TIMEOUT_SECONDS = _env_float("ORS_TIMEOUT_SECONDS", 30)
ORS_MIN_INTERVAL_SECONDS = _env_float("ORS_MIN_INTERVAL_SECONDS", 0)  # the plan's limit is per minute, not per second

# The truck the ORS route is planned for (metres and metric tonnes). Defaults: a US 5-axle tractor-trailer at the
# usual legal limits. Roads that do not allow this truck (low bridges, weight limits) are avoided, as far as
# OpenStreetMap has them tagged. AXLE_LOAD_T is left unset because most roads carry no axle limit.
TRUCK = {
    "HEIGHT_M": _env_float("TRUCK_HEIGHT_M", 4.11),  # 13 ft 6 in
    "WIDTH_M": _env_float("TRUCK_WIDTH_M", 2.59),  # 8 ft 6 in
    "LENGTH_M": _env_float("TRUCK_LENGTH_M", 22.0),  # about 72 ft
    "WEIGHT_T": _env_float("TRUCK_WEIGHT_T", 36.3),  # 80,000 lb
    "AXLE_LOAD_T": _env_float("TRUCK_AXLE_LOAD_T", 0) or None,
    "HAZMAT": os.environ.get("TRUCK_HAZMAT", "0") == "1",
}

if ROUTING_ENGINE not in ("osrm", "ors"):
    raise ImproperlyConfigured(f"ROUTING_ENGINE must be 'osrm' or 'ors', not {ROUTING_ENGINE!r}.")
if ROUTING_ENGINE == "ors" and not ORS_API_KEY:
    raise ImproperlyConfigured(
        "ROUTING_ENGINE=ors needs an OpenRouteService API key: put ORS_API_KEY=your-key in the .env file next to "
        "manage.py (or set the ORS_API_KEY environment variable). Free sign-up: "
        "https://openrouteservice.org/dev/#/signup. Or remove ROUTING_ENGINE to use OSRM."
    )

# Geocoding fallback for inputs that are neither "lat,lon" nor "City, ST".
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
NOMINATIM_ENABLED = os.environ.get("NOMINATIM_ENABLED", "1") == "1"
NOMINATIM_TIMEOUT_SECONDS = _env_float("NOMINATIM_TIMEOUT_SECONDS", 10)
# Nominatim's usage policy requires an identifying User-Agent.
HTTP_USER_AGENT = os.environ.get("HTTP_USER_AGENT", "fuel-route-planner/1.0 (take-home exercise)")
