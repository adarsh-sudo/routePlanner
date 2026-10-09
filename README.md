# Fuel-optimized route API

Give it a start and a finish in the USA. It returns the driving route, the cheapest places to
fuel up along it for a vehicle with a **500-mile range at 10 mpg**, and the **total money spent
on fuel**. Built on **Django 6.1** (latest stable).

* Two calls to the routing API per new route: one for the route, one measuring the real driven detour to
  the chosen fuel stops. Both cached afterwards. With a free OpenRouteService key the routes are planned for a
  **truck** (see [Truck routes](#truck-routes)). With no key the app still runs, on the free OSRM server, which
  plans **car** routes, and the page says so.
* Station prices come from `fuel-prices-for-be-assessment.csv` (bundled at `routeplanner/data/`).
  Station positions come from OpenStreetMap and Overture Maps, matched once, offline (see
  [Data preparation](#data-preparation)).
* Warm requests take ~30 ms. A cold cross-country request is ~1.5-2.5 s, nearly all of it the two routing
  round trips (OSRM or OpenRouteService); everything this app does itself is ~0.1 s (see [Performance](#performance)).

## Quick start

Requires Python 3.12+ (Django 6 requirement).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_stations                          # offline, ~1 s
python manage.py runserver
```

That runs out of the box on the free OSRM server, which plans **car** routes, and the page says so. For **truck**
routes, copy `.env.example` to `.env`, add a free OpenRouteService key and restart (see
[Truck routes](#truck-routes)).

```bash
curl "http://127.0.0.1:8000/api/route/?start=Los Angeles, CA&finish=New York, NY"
```

Open the map in a browser: <http://127.0.0.1:8000/api/route/map/> and type a start and finish into the
boxes (or deep-link: <http://127.0.0.1:8000/api/route/map/?start=Los%20Angeles,%20CA&finish=New%20York,%20NY>).

Run the tests: `python manage.py test` (247 tests, no network needed).

## API

`GET /api/route/?start=...&finish=...` (also `POST` with a JSON body `{"start": ..., "finish": ...}`)

`start` / `finish` accept any of:

| Format | Example | External calls |
|---|---|---|
| Coordinates | `39.7392,-104.9903` | 0 |
| City and state | `Denver, CO` / `Denver, Colorado` / `Denver CO` | 0 (offline gazetteer) |
| Any US address | `1600 Pennsylvania Ave NW, Washington, DC` | 1 geocode each (Nominatim, cached) |
| Place name, no state | `Chicago`, `India` | 0 (offline gazetteer) |
| Landmark | `Mount Rushmore` | 1 geocode each (Nominatim, cached) |

**Every named US settlement is known offline** (~210k names: Census incorporated places, CDPs and
townships, plus USGS GNIS hamlets and other unincorporated communities), and that is where their
coordinates come from. Input is validated against it rather than blindly geocoded:

* A name with a state (`India, PA`) resolves to exactly that place.
* A name **without** a state resolves to the **most populous** US place of that name (Census population
  estimates): `Miami` is Miami, FL (~490,000), not Miami, KS (a township of ~560), and `Atlanta`, `Cleveland`,
  `Buffalo`, `Newark` and `Oakland` mean Georgia, Ohio, New York, New Jersey and California. (It used to rank by
  land area, which sent 16 of 47 well-known names to a tiny same-named place in another state.) Where a name
  has no population estimate (a CDP or unincorporated community) it ranks behind places that do, by land
  area. `India` is a hamlet in four states, so it resolves to one of them, and the response says so:
  `finish.resolved_address` explains (`"India, MS is one of 4 US places named India; add a state..."`) and
  `finish.alternatives` lists the others, most populous first. The map page shows the same note with the
  alternatives as one-tap links that re-run the search with that place named in full. Add a state to pick a
  different one.
* A name that is not a US settlement (`xyz`, a trail in Georgia) can only be a landmark or address, so it
  is accepted only if the geocoder says it is well known (Nominatim `importance` >= 0.3, e.g. Mount
  Rushmore 0.55; `xyz` scores 0.04) or has a street number / ZIP.
* Foreign places are rejected without any lookup: `Paris, France`, `Calgary, AB`, and country names
  that are not also US places (`United Kingdom`).

So a typical request makes **2** calls to the routing API (route + detour measurement). Two street
addresses would make 4, so the detour call is skipped once a request has used the ceiling of **3**
external calls (`FUEL_MAX_EXTERNAL_CALLS`): 2 geocodes + 1 route, with straight-line detours. The same
happens, with a normal answer, if the detour call fails. `api_calls` in every response reports the actual count.

`GET /api/route/map/?start=...&finish=...` renders the same result as an interactive Leaflet map
(route line, lettered start/finish pins, numbered fuel-stop pins with popups, and a cost summary).
The page is a single self-contained template built to a private design brief (`frontend.md`, kept out of this repo; the
decisions are recorded in `CLAUDE.md`),
in a powder palette with an automatic dark mode: a docked planner beside the map with Start/Finish boxes
and a swap button, a plain-sentence answer ("Seattle, WA to Miami, FL needs $1,041.62 of fuel"), a
tank-level chart showing fuel falling between stops and jumping where it is bought, and a stop list whose
pins are tinted by price tier (cheap = mint, middle = butter, highest = blush). Hovering or clicking a
stop, a chart dot or a pin highlights the others. Opened with no parameters it shows a sample-trip
empty state; input errors appear in the page with a fix hint (same HTTP status as the JSON API), and
a skeleton of the result shows while a cold route (about 2 s) is worked out. It is keyboard operable,
respects reduced motion, and stacks on phones (map on top, planner below). On a touch screen the map pans
so a popup always fits (heading included), the zoom buttons move to the top-right (hidden under 360 px
wide, where pinch zoom is left), and tapping a pin no longer scrolls the page to the list. These came out of
a phone-emulation test in Chrome (Pixel 7 at 412 px and iPhone SE at 320 px wide, with touch input,
tapping rows, pins and buttons, light and dark mode), which found popups cut off at the top of the short
phone map and pins sitting under the credit line; it passes on both sizes. It is an emulation, not a real
phone.
The basemap is Esri's gray canvas, which needs no API key (OSM's own tile server blocks requests
without a Referer, and CARTO's now require a key).
The JSON response includes a ready-made `map_url`, plus the route as a GeoJSON `LineString`
(`route.geometry`) for use with any map library.

### Taking the plan into a navigation app

This app plans where to fill up; it does not navigate (no turn-by-turn, no live position). The response
carries the plan into software that does, as a `navigation` block that the map page also shows under
"Take this plan with you":

* **Google Maps links** (`navigation.google_maps`, and `google_maps_mobile` for phones): directions URLs with
  the fuel stops as waypoints, in order. Google's [documented limits](https://developers.google.com/maps/documentation/urls/get-started)
  are 9 waypoints per link, **3 on mobile browsers**, and 2,048 characters per URL, so a longer plan is split
  into parts that each start where the last one ended (LA -> New York is one link on a desktop, two on a
  phone). Stops matched to a station or exit are sent as exact coordinates; a stop still at its city centre, or
  one whose station our two map sources dispute (`uncertain`), has only a guessed position, so it is sent by
  name and town, which Google resolves to the real business.
* **GPX file** (`navigation.gpx_url`, i.e. `GET /api/route/gpx/?start=...&finish=...`): the standard exchange
  format for GPS devices and apps (GPX 1.1). Each stop is a waypoint with its price and what to buy, and
  start -> stops -> finish is a route, so a GPS that supports it can route between the stops with its own
  settings, for example a truck profile. A stop with only a guessed position (city centre, or `uncertain`) is a
  waypoint with a warning but not a route point, because a GPS would drive to the guess.

What was checked: the generated LA -> New York link was opened in Google Maps on a desktop and gave the
full route through all six stops (2,812 miles, via I-15 N). The stops matched to a station came back as
an address at the station (Green River, UT as "Maverik, 1475 W Main St"), and the guessed Waco, NE stop,
sent by name, came back as the real "Akal Travel Center". The first stop is a case where the data used to be
wrong: with only OpenStreetMap, Maverik #674 in North Las Vegas was placed at its highway exit and Google
labelled that point "2300 Crestline Loop", a building. OpenStreetMap has no Maverik there, but Overture Maps
lists "Maverik #674" itself, and with it the stop now comes back as "1900 Losee Rd", the same block as the
"Maverik, 1970 Losee Rd" Google finds by name. Stops still at their highway exit (sent as the exit's
coordinates) come back as an address at the exit, once just "I-80, North Utica, IL", so the driver still has
to spot the station there. Two variations were tried and rejected: adding the price file's highway and exit
text to a stop ("Maverik #674, I-15, Exit 45, ...") made Google fail to find it, and sending exit-level stops
by name alone resolved but can pick a different store of the same chain in a big town. On a phone-sized
screen (emulated) the page offers the two-part links and the buttons are 44 px or taller. Not checked: a real
phone, the native Google Maps app, a physical GPS or truck unit, or the GPX against the official schema. Google
plans its own car route between the stops, and nothing here adds a fuel reserve (see the upgrade plan).

### Response (trimmed; real output)

```jsonc
{
  "start":  { "query": "Los Angeles, CA", "label": "Los Angeles, CA", "lat": 34.01939, "lon": -118.41083, "resolved_by": "gazetteer" },
  "finish": { "query": "New York, NY",    "label": "New York, NY",    "lat": 40.66271, "lon": -73.93868, "resolved_by": "gazetteer" },
  "route": {
    "distance_miles": 2810.7,
    "duration_hours": 50.39,
    "geometry": { "type": "LineString", "coordinates": [[-118.41035, 34.01965], /* ...4,488 points... */ [-73.93872, 40.6631]] }
  },
  "routing": { "engine": "osrm", "vehicle": "car", "credit": "OSRM", "truck": null,
               "note": "This is a car route, not a truck route. Add an OpenRouteService key (ORS_API_KEY) to plan for a truck." },
               // with a key: engine "openrouteservice", vehicle "truck", the truck's size, note null (see "Truck routes")
  "start_fill": {                       // the fuel used from the starting tank, see "Cost model"
    "reference_station": { "name": "Shelee's Travel Center", "city": "Coachella", "state": "CA", "location": "site", "miles_from_origin": 129.8, /* ... */ },
    "price_per_gallon": 3.949, "gallons": 28.4, "cost": 112.15
  },
  "fuel_stops": [
    { "order": 1, "station_id": 72965, "name": "Maverik #674", "address": "I-15, Exit 45",
      "city": "North Las Vegas", "state": "NV", "lat": 36.19511, "lon": -115.14182, "location": "site",
      "price_per_gallon": 3.282, "mile_marker": 282.9, "miles_off_route": 0.2,
      "detour_miles": 0.82, "detour_source": "driven", "gallons": 49.88, "detour_gallons": 0.08, "cost": 163.73 },
    { "order": 2, "name": "MAVERIK COUNTRY STORE #693", "city": "Green River", "state": "UT", "location": "site",
      "mile_marker": 685.2, "miles_off_route": 0.3, "detour_miles": 1.49, "detour_source": "driven", /* ... */ }
    /* ...4 more... */
  ],
  "summary": { "total_gallons": 282.44, "detour_gallons": 1.37, "total_fuel_cost": 893.11, "num_fuel_stops": 6,
               "range_miles": 500.0, "mpg": 10.0, "candidate_stations_considered": 364, "stop_penalty_usd": 10.0,
               "driven_detours": true },
  "map_url": "/api/route/map/?start=Los+Angeles%2C+CA&finish=New+York%2C+NY",
  "api_calls": { "routing": 2, "geocoding": 0 },
  "timing_ms": 2245.6
}
```

`summary.total_fuel_cost` is **the total money spent on fuel**. `total_gallons` is `distance / 10` plus
`detour_gallons`, the fuel burnt driving off the route to the chosen stations (see "Cost model").
For each stop, `gallons` is what is bought there, `detour_gallons` is what the round trip to its pumps
burns, `miles_off_route` is the straight-line distance to the route, and `detour_miles` is the round
trip the planner charged: **driven** (measured by road) or `straight_line` (twice the straight-line
distance; see "How it works"). Each station also carries `location`: `site` (matched to its real fuel
station on OpenStreetMap or Overture Maps), `uncertain` (matched to a station, but OpenStreetMap and Overture,
each on its own, pick stations more than 2 miles apart, so it may be a different store of the same chain),
`exit` (placed at its highway exit) or `city` (centre of its city, the fallback).

### Errors

JSON `{"error": {"code": ..., "message": ...}}`

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_location` | missing/unparseable input, or not in the USA (e.g. `Calgary, AB`) |
| 422 | `no_feasible_route` | no drivable route, or a stretch longer than 500 miles with no station |
| 422 | `route_too_long` | truck mode only: the trip is over OpenRouteService's free-plan limit of 6,000 km |
| 502 | `upstream_unavailable` | the routing or geocoding service failed (in truck mode also: key refused, rate limit) |
| 503 | `stations_not_loaded` | `load_stations` hasn't been run |

## How it works

1. **Resolve** start/finish (coordinates -> parsed; `City, ST` -> offline gazetteer; else Nominatim).
2. **Route**: one routing call (OSRM: `overview=full&geometries=polyline6`, ~5x smaller and faster than
   GeoJSON; OpenRouteService: its default encoded polyline). The polyline is decoded, thinned to ~0.5 mi spacing with running mile markers, and
   hashed into a grid. The result is cached in-process by rounded coordinates.
3. **Candidates**: every station within 10 miles of the route is projected onto it to get a mile
   marker and an off-route distance. Of the stations sharing a mile marker, one is dropped only if
   another is both at least as cheap and at least as close to the road, so a pricier station that is
   right on the route survives next to a cheaper one far off it.
4. **Optimize**: an exact dynamic programme over (station, fuel level) picks where to stop and how
   much to buy (`routeplanner/services/planner.py`). It is vectorised with numpy, so even a
   coast-to-coast route takes a few milliseconds. This first pass charges each detour at twice the
   straight-line distance to the route.
5. **Measure detours by road**: one OSRM `table` call (OpenRouteService: `matrix`) measures the real driven
   detour to the chosen stops and to the stations the plan is most likely to swap to (10 stations by
   default, 3 coordinates each; the public OSRM server accepts 100, OpenRouteService's free plan 16). For a station S, A and B are the route points 10 miles
   before and after where S meets the route, and the detour is `d(A,S) + d(S,B) - d(A,B)`: the extra
   miles of driving A -> S -> B instead of A -> B. The shared highway cancels, so it does not matter
   which exits the driver leaves and rejoins at. Then the plan is made again with those distances.
   Only stations matched to their real fuel station (`location: site`) are measured: a city centre,
   or an exit point that can sit on the wrong carriageway and invent a U-turn, would give a
   meaningless road distance, so those keep the straight-line estimate. A stand-in the call did not
   cover also keeps it, and the response says which (`detour_source`).

### Cost model (and its assumptions)

* The vehicle starts with a **full 500-mile tank**; every mile is paid for at the price it was
  bought at. The fuel used from the starting tank is priced at the **cheapest station within
  50 miles of the origin** (nearest station if none is that close) and reported as `start_fill`.
  This means short trips still have a real cost.
* The trip is planned to arrive with an empty tank (no reserve).
* **Detours cost fuel.** A station `d` miles off the route is a `2d`-mile round trip, which burns
  `2d / 10` gallons that must be bought on top of what the route needs. The way out is driven on fuel
  already in the tank and the way back on fuel bought at the station, so each gallon is priced where it
  was actually bought. The detour also limits the fill: the vehicle needs `d` miles of range to reach the
  pumps, and rejoins the route `d` miles short of a full tank. The optimizer weighs all of this against
  the price, so a cheaper station far off the road loses to a slightly dearer one on it unless the saving
  covers the detour. The detour is the driven extra distance when it could be measured, otherwise twice
  the straight-line distance, which understates a winding road. The time a detour takes is not priced.
* **Stop penalty.** The objective is *fuel cost + $10 per stop*. A pure "cheapest fuel" optimizer
  hops between stations buying a gallon or two to save cents (17 stops for LA -> NYC, which saves
  $9). The penalty means a stop is only made when it saves more than $10 in fuel. Tune with
  `FUEL_STOP_PENALTY_USD` (0 = pure cheapest-fuel plan). `total_fuel_cost` excludes the penalty.
* A truck stop with several prices in the file is collapsed to its **lowest** listed price.

### Data preparation

The price file has no coordinates, so station positions are worked out **once, offline** (the API never
geocodes stations at request time):

* `build_places` (already run; its output ships in `routeplanner/data/`) builds `routeplanner/data/us_places.csv` from the
  public-domain US Census Gazetteer, `us_communities.csv` (131k unincorporated communities) from the
  public-domain USGS GNIS populated places, and geocodes the 129 station cities the Census file
  misses via Nominatim into `station_geocode_overrides.json`. `us_places.csv` also carries a `pop` column
  (Census 2025 population estimates for incorporated places, consolidated cities and townships, 29,931 names):
  it ranks same-named places for a name typed without a state. `python manage.py build_places --population-only`
  adds or refreshes it without redoing the rest.
* The first request that needs the full name index builds it (~0.6 s, ~65 MB, once per process).
* Two open map sources give the stations (`services/station_match.py` matches each stop against them):
  * **OpenStreetMap** (Overpass API, `build_station_coords`): every fuel station and numbered motorway exit,
    per state, kept gzipped in `.osm_cache/` (12 MB).
  * **Overture Maps** (`fetch_overture_places`, needs `pip install duckdb`, for this command only): fuel and
    convenience-store listings, partly from Foursquare, which include stations OpenStreetMap lacks, sometimes
    with the store number in the name ("Maverik #674"). The 237,030 places within 12 miles of a stop ship as
    `routeplanner/data/overture_places.csv.gz` (8.5 MB; release in `overture_places.release`). Places Overture
    marks closed are dropped, and a place already on OpenStreetMap is merged into it, keeping OpenStreetMap's
    position. Overture is not a drop-in: its entry for a station may be categorised as a convenience store, so
    matching is by name, not category.
* `build_station_coords` (already run; its output `station_coords.csv` ships in `routeplanner/data/`) finds
  each stop's real position. The file gives a highway exit and a city, e.g. `I-44, EXIT 283 & US-69`, and
  only 149 of the 6,626 stops look like a street address, so a street geocoder cannot help.
  `python manage.py build_station_coords --offline` rebuilds everything from the two shipped data sets:
  1. With a highway and exit number, it finds that exit (highway `I 44` + exit `283`). A fuel station
     whose name or brand fits the stop (a matching store number counts for more) within 2.5 miles of the
     exit is the stop's **site**; with none, the exit point itself is used (**exit**).
  2. With no exit number, a name-matched station within 8 miles of the city centre is used, and each
     station is given to one stop only, so three stores of one chain in a town cannot all pick the same pump.
  3. Anything else keeps its city centre (**city**). An exit more than 25 miles from the stated city is
     treated as an error in the file (one RaceTrac listed under Alabaster, AL cites an exit 44 miles away).
  Between equally named stations the nearer wins, but an Overture listing must be 1.5 miles nearer than an
  OpenStreetMap one to beat it (a matching store number overrides this).
  4. **Uncertain.** Each source is also run alone (`locate_state`). If the two pick stations more than 2 miles
     apart for a stop, it is marked `uncertain`: it is one of several stores of a chain and the price file
     (a road, an intersection, a city) cannot say which. The position stays the merged pick; the flag only
     says not to trust it. One source finding nothing is not a disagreement.
  Map data (c) OpenStreetMap contributors (ODbL); places from Overture Maps (CDLA-Permissive-2.0; the Places
  theme combines sources including Foursquare, Apache-2.0).
* **How accurate is it?** Two measurements, both against stops whose store number (`#1243`) appears on exactly
  one same-brand OpenStreetMap station (293 stops, two thirds Speedway, the chain whose numbers are on the
  map), with the matcher not allowed to use the number:
  * *Where OpenStreetMap has the station:* median error **0.0 mi**, 90th percentile 4.0 mi, **75% within
    1 mile**, against a median 2.3 mi and 24% within 1 mile for the city centre (OpenStreetMap alone: 77%;
    the extra Overture candidates cost two points). Stops placed at an exit sit a median 0.4 mi (90th
    percentile 1.1 mi) from the real station (130 stops).
  * *Where it does not* (each stop's own OpenStreetMap station deleted, to see what Overture recovers):
    median error 1.29 mi -> **0.05 mi**, within 0.3 mi 18% -> **55%**, within 1 mile 44% -> **62%**.
    Overture lists a same-brand place within 0.3 mi of 97% of the stations the key proves exist, so its
    positions agree with OpenStreetMap's.
  * *Does the `uncertain` flag mean anything?* On the two answer keys together (449 stops with a station match,
    store numbers hidden), unflagged stops were wrong by more than 2 miles 16% of the time (median error
    0.0 mi, 79% within 1 mile); the 22 flagged ones 59% of the time (median 3.9 mi, 36% within 1 mile). That
    is a small flagged sample.
  The key is biased towards chains with numbers, the search radii were tuned on it, and Overture-only matches
  have no independent check beyond this simulation. The Maverik case that exposed the gap now resolves in
  Google Maps to the right block (see above).
* `load_stations` drops the ~600 Canadian rows, dedupes to 6,626 truck stops, and loads **6,620** into
  SQLite: **5,244** at their station, **395** at a station that is `uncertain` (2,544 of the 5,639 station
  matches came through Overture), **478** at their exit, **503** at their city centre. 6 stops (0.1%, e.g. "Hot Springs National Park, AR") have no resolvable city and are skipped.

## Limitations

* **Station positions are exact only for some stops.** 79% are matched to their station, 6% to a station
  but `uncertain` (the two sources disagree, so it may be a different store of the chain), 7% sit at their
  highway exit and 8% still use their city centre. Of the city-centre stops, 91% have no exit number
  (US- and state-road stops) and no station of that name within 8 miles in either source, so only another
  data source, such as road geometry, would place them; the exit-level ones have an exit number but no
  name-matched station within 2.5 miles of it. Each station says which kind it is in `location`, and the map
  page says so in its popup. A stop that is not matched to a station can be miles from its real pump (a
  city-centre one most of all), so its `mile_marker` and detour are rough; the 10-mile search radius stays
  generous for that reason. The plan can still choose a stop it has flagged (on Boston -> Dallas, all four
  stops are `uncertain` or exit-level): it warns, it does not avoid them.
* **Detours are measured by road only for `site` stations** (see "How it works"). Stops at an exit or city
  centre, `uncertain` ones, and stand-ins the measurement did not cover, use twice the straight-line
  distance. The detour's time is not priced, only its fuel.
* **Sparse data in places**: the file has only 16 California stops, so for a Los Angeles origin the
  nearest reference station is 130 miles away (the first tank is priced at $3.949, the dearest fuel on
  the trip). The reference station is reported so this is visible.
* The public OSRM server is for light use and cars only (not truck-specific routes); set `OSRM_BASE_URL`
  to a self-hosted instance for heavy request volume, or plan truck routes with OpenRouteService (see
  [Truck routes](#truck-routes)). Routes ignore road traffic (see the upgrade plan).
  Prices are a static snapshot. Contiguous US only.

## Truck routes

This is a truck planner, so the routes are planned for a truck, using [OpenRouteService](https://openrouteservice.org)
(ORS) and its `driving-hgv` (heavy goods vehicle) profile. That needs a free key. **With no key the app still runs**
(so it works out of the box, for example for someone trying it for the first time), but on the free OSRM server,
which plans **car** routes: it does not know that a truck cannot use a low bridge, a weight-limited road or a parkway
that bans trucks. In that case the page shows a note above the answer ("This is a car route, not a truck route. Add
an OpenRouteService key...") and the JSON says `routing.vehicle: "car"` with the same sentence in `routing.note`.

**Turn it on.** Sign up for a free key at <https://openrouteservice.org/dev/#/signup>, then put it in a private
`.env` file in the folder with `manage.py` (copy `.env.example`, which shows every setting):

```
ORS_API_KEY=paste-your-key-here
```

then restart the server. The key is all it takes: truck routing switches on by itself when there is one. `.env` is in `.gitignore`, so it is never committed; never put the key in the code, a URL
or a chat. `python manage.py check_ors` makes two real requests and tells you if the key and the whole truck path
work. Notes on how the file is read:
* A real environment variable wins over the file (`$env:ORS_API_KEY = "..."` in PowerShell, `export` in bash), and
  a blank value in the file is ignored. Any other setting in "Configuration" can go in `.env` too.
* The file can be UTF-8 or the UTF-16 that PowerShell's `>` writes. `DOTENV_PATH` points at a different file.
* `python manage.py test` ignores the default `.env`, so your own settings can neither block nor change the tests.
* `ROUTING_ENGINE=osrm` forces car routes even when there is a key (handy for comparing the two);
  `ROUTING_ENGINE=ors` with no key makes the server refuse to start and say where to put it. A blank value counts
  as not set. To go back to car routes, remove the key or set `ROUTING_ENGINE=osrm`.

**What changes**
* The route, and the detour measurement, are planned for a truck: by default a US five-axle tractor-trailer,
  13 ft 6 in high, 8 ft 6 in wide, 72 ft long, 80,000 lb. Change it with `TRUCK_HEIGHT_M`, `TRUCK_WIDTH_M`,
  `TRUCK_LENGTH_M`, `TRUCK_WEIGHT_T`, `TRUCK_AXLE_LOAD_T` (unset by default) and `TRUCK_HAZMAT=1` (metres and
  metric tonnes). Roads that do not allow that truck are avoided, **as far as OpenStreetMap has the limits tagged**,
  which is patchy: this is not a substitute for posted signs or a truck GPS.
* The JSON gains `routing: {"engine": "openrouteservice", "vehicle": "truck", "truck": {...}}`, the page says
  "Routed for a truck 13 ft 6 in high, 80,000 lb." under the headline, and the footer credits
  "openrouteservice.org by HeiGIT" (ORS asks for this; its map data is OpenStreetMap's).
* The Google Maps links still plan a car route between the stops (the page says so). Google's truck routing is a
  developer product, not part of the Google Maps app or its links (see the upgrade plan).

**Free-plan limits** (as ORS publishes them; check your account): 2,000 route requests and 500 matrix requests a
day, 40 a minute, routes up to 6,000 km (3,728 miles). A search uses one of each, so about 500 searches a day. A
trip over the distance limit answers 422 `route_too_long`. When the matrix quota runs out, searches still work
but fall back to straight-line detours (`summary.driven_detours` is `false`). Whether the free plan may be used
commercially is not something I could confirm: check ORS's terms before relying on it for a business.

**What was checked.** The requests and answers follow ORS's published documentation and are covered by
`tests/test_ors.py`. The whole path was first run over real HTTP against a stand-in ORS server (key header, JSON
bodies, longitude-first coordinates, truck restrictions, error answers), then, on 9 October 2026, against the
**real service** with a free key (Seattle -> Dallas and Denver -> Chicago). Both the route request (including
`radiuses: [-1, -1]`) and the matrix request were accepted, detours were measured by road, and the plan came back.
The truck profile is in effect: Seattle -> Dallas takes 46.8 h by truck against 37.0 h by car on OSRM, and the
distance differs slightly (2,086 against 2,083 miles). Still not confirmed: that a truck is kept off a particular
low bridge or weight-limited road (nothing here tests a real restriction), what the matrix returns for a pair with
no route (the code reads `null` as "no route"), and the free plan's terms for commercial use. Run
`python manage.py check_ors` to re-check a key.

## Upgrade plan (not implemented)

The brief takes exactly two inputs (start and finish), so these are deliberately left out of the API.
The optimizer already tracks fuel level, so items 1-3 are small changes; item 4 needs a traffic data source:

1. **Starting fuel level** (e.g. `start_fuel=0.4`): the car currently always starts full.
2. **Reserve** (e.g. 15-25%): today the plan arrives at every stop and the destination with an empty
   tank, which is unrealistic given that station positions can be off, a station can be closed, and 15% of
   stops are still only placed at their exit or city centre.
3. **Refuel time**: replace the flat `FUEL_STOP_PENALTY_USD` with minutes per stop x value of time, and
   report stop time and total trip time (driving + stops).
4. **Traffic-aware routing**: today the route is the single fastest path by the routing engine's static speed model
   (OSRM's, or OpenRouteService's truck profile; `routes[0]`, no live or typical traffic), so `duration_hours` is a free-flow estimate and a jam never
   changes the route. It needs no new inputs, only a different routing source:
   * Ask for several candidate routes (OSRM `alternatives=true`, or a traffic provider such as Google
     Routes, Mapbox Directions `driving-traffic`, TomTom or HERE, which need an API key).
   * Run the existing candidate search and optimizer on **each** route (a few ms each) and keep the one
     with the lowest *fuel cost + value of time x traffic-adjusted duration*. Without the time term the
     cheapest-fuel route would usually win and traffic would never matter, so this builds on item 3.
   * Return the chosen route plus the alternatives with their cost and time, and show them on the map.
   * Cache with a short TTL for live traffic. The route cache today keeps routes for 24 hours, which is
     right for a static model and wrong for live conditions.
   * Stop-and-go driving also burns more fuel than the fixed 10 mpg assumes; modelling that is optional.
5. **Truck routing beyond the OpenRouteService free plan** (the free-plan version is built: see
   [Truck routes](#truck-routes)). Options looked at in October 2026:
   * **Google Routes API, Large Vehicle Routing** (`travelMode: TRUCK` plus `routeModifiers.vehicleInfo`: height,
     length, weight, axles, hazmat): generally available for the contiguous US per Google's documentation, but
     "available to a limited set of customers" (Google has to enable your project), with no price on that page. It
     does not cover truck toll prices, speed limits or radioactive hazmat, and flags routes that partly ignore a
     restriction (`routeRestrictionsPartiallyIgnored`), which Google says not to treat as a single source of truth.
     It is a developer product: the Google Maps app and the links this page makes still plan car routes.
     ([docs](https://developers.google.com/maps/documentation/routes/lvr))
   * **HERE Routing v8** (`transportMode=truck`) has a free tier; the sources found disagree on its size.
   * **Self-hosted Valhalla or GraphHopper** (both have truck profiles) on an OpenStreetMap US extract: no request
     or distance limits, but you run the server.
   * **Not usable**: the public Valhalla server (`valhalla1.openstreetmap.de`) rejects any route over 1,500 km
     (tried with Seattle -> Dallas: HTTP 400, error 154), so it cannot plan these trips.

Considered and rejected: a threshold rule such as "refuel when a quarter of the tank is used and pick a
nearby cheap pump". Compared against the optimal plan on four routes (same stations, same detour
estimates, 500 mi, 10 mpg) it cost 1-10% more, and the "25% used" variant made 2-2.6x as many stops:

| Route | Optimal | Refuel at 25% used | Refuel at 25% left |
|---|---|---|---|
| Seattle -> Miami | $1,044.52, 7 stops | $1,095.83, 18 stops (+4.9%) | $1,099.05, 7 stops (+5.2%) |
| Los Angeles -> New York | $892.60, 6 stops | $979.33, 15 stops (+9.7%) | $940.20, 6 stops (+5.3%) |
| Boston -> Dallas | $529.02, 4 stops | $576.49, 9 stops (+9.0%) | $575.93, 4 stops (+8.9%) |
| Denver -> Chicago | $296.11, 2 stops | $311.34, 4 stops (+5.1%) | $299.50, 2 stops (+1.1%) |

The rule (`services/baselines.py`): the vehicle starts full; when the tank reaches the trigger level (75%
of the range for "25% used", 25% for "25% left") it looks at the stations from there up to 50 miles on,
takes the cheapest, and fills the tank; with none in that window it takes the nearest it can reach; it
does not stop if the finish is already within reach. Fuel is costed first in, first out, as for the
optimal plan, and fuel left at the finish is free. Regenerate the table with
`python manage.py compare_rules` (it calls the live routing service). All three columns use the
straight-line detour estimates so the comparison is like for like; the product itself, which also
measures detours by road, costs $1,044.38 (7 stops), $893.11 (6), $529.02 (4) and $296.26 (2) on the same
routes (OSRM car routes; a truck route differs a little, for example Seattle -> Miami is $1,041.62 with 7 stops). This table replaces an earlier one, from a script no longer in the repo, that used city-level
positions and free detours; its ranking was the same (2-9% more, up to 2.5x the stops).

## Performance

Measured for Los Angeles -> New York (2,811 mi) on the dev machine:

| Step | Time |
|---|---|
| OSRM route round trip (not under our control) | ~0.5-2 s, depending on the route and the server's load |
| ...extra if it has to open a new connection | ~0.8 s |
| OSRM detour measurement (not under our control) | ~0.3 s for 10 stations, ~0.7 s for 30 |
| Decode 34,351-point polyline | 44 ms |
| Thin to 4,488 points + build grid | 31 ms |
| Find 364 candidate stations | 20 ms |
| DP optimizer (two passes, 9 ms each) | 18 ms |
| **Everything this app does** | **~110 ms** |
| Repeat of the same route (cached) | ~30 ms |

These were measured with OSRM (car routes). With OpenRouteService (truck routes, the real service, 9 October 2026) a
cold search took 2.3 s for Seattle -> Dallas (2,086 mi), 1.0 s for Seattle -> Miami, OH and 0.7 s for Denver -> Chicago:
the same range, since the routing service is again nearly all of it. The courtesy pause described below applies to
OSRM only.

A cold request therefore takes about 1.5-2.5 s, almost all of it the two routing round trips (the public
OSRM server's speed varies from run to run, by up to ±0.7 s). What was done about it, measured on fresh server
processes (first search after a restart, Los Angeles -> New York, same $893.11 plan): **~2.6 s -> ~1.5 s**.

* **Fewer stations in the detour call**: 10 instead of 30 saves ~0.35 s. On 12 routes it picked the same stops
  as 30, within $0.07 of the cost.
* **Connection warm-up**: opening a connection to the routing server costs ~0.8 s (a first request took ~1.0 s,
  the next on the same connection ~0.2 s), and an idle connection stays usable for 45 s but had closed by
  90 s. So the server opens one at start-up (`services/warmup.py`, in a background thread, along with the station
  table and place indexes) and the page asks it to refresh one (`GET /api/route/warm/`) when the user first
  focuses a box or points at a sample trip. With several server workers each keeps its own connection, and a
  warm-up request only warms the worker that answers it, so the gain is smaller there.
* **Not parallel, on purpose**: the detour call needs the route (to place its A and B points) and the plan (to
  know which stops to measure), so the two calls cannot overlap, and the plan between them takes ~10 ms.
  Splitting the detour call into two concurrent halves saved only ~0.15 s (449 vs 605 ms for 30 stations) and
  sends two requests at once, against the public server's one-request-per-second rule.
* **Still on the table**: the courtesy pause (`OSRM_MIN_INTERVAL_SECONDS`) can delay the detour call by up to
  ~0.4 s when the route comes back in under a second. A self-hosted OSRM (`OSRM_BASE_URL`) removes the pause,
  the rate limit and most of the round-trip time.

The station table (6,620 rows) is loaded into memory once per process (~30 ms).

## Configuration

Environment variables, or the same `NAME=value` lines in a `.env` file (see [Truck routes](#truck-routes);
defaults in `config/settings.py`):
`FUEL_RANGE_MILES` (500), `FUEL_MPG` (10), `FUEL_SEARCH_RADIUS_MILES` (10), `FUEL_STOP_PENALTY_USD` (10),
`FUEL_START_PRICE_RADIUS_MILES` (50), `OSRM_BASE_URL`, `NOMINATIM_ENABLED` (1), `DJANGO_SECRET_KEY`,
`DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`.

Routing engine: `ORS_API_KEY` (truck routes), `ROUTING_ENGINE` (`ors` when there is a key, else `osrm`: car routes;
set it to force one), `ORS_BASE_URL` (`https://api.openrouteservice.org`), `ORS_TIMEOUT_SECONDS` (30),
`ORS_MIN_INTERVAL_SECONDS` (0; the free plan's limit is per minute). The truck: `TRUCK_HEIGHT_M` (4.11),
`TRUCK_WIDTH_M` (2.59), `TRUCK_LENGTH_M` (22), `TRUCK_WEIGHT_T` (36.3), `TRUCK_AXLE_LOAD_T` (unset),
`TRUCK_HAZMAT` (0).

Detour measurement: `FUEL_DRIVEN_DETOURS` (1; 0 = straight-line detours only, one routing call),
`FUEL_MAX_EXTERNAL_CALLS` (3), `FUEL_DETOUR_SPAN_MILES` (10), `FUEL_DETOUR_CHECK_LIMIT` (10; at most 33),
`OSRM_MIN_INTERVAL_SECONDS` (1; the public server asks for no more than one request per second).

Start-up: `FUEL_WARM_ON_START` (1; 0 skips the background warm-up, e.g. to avoid building the ~65 MB place-name
index before it is needed).

## Layout

```
config/                     Django project (lean: no admin/auth/sessions); envfile.py reads the .env file
routeplanner/
  models.py                 Station
  views.py, urls.py         /api/route/, /api/route/map/, /api/route/gpx/
  services/
    places.py               input -> coordinates (offline first)
    routing.py              routing client (OSRM or OpenRouteService): route, driven-detour measurement, caches
    ors.py                  OpenRouteService truck routing: request bodies, answers, errors (pure)
    geo.py                  distances, polyline decode, route thinning, spatial grid
    stations.py             CSV parsing, in-memory station index
    station_match.py        truck stop -> real map position, from OpenStreetMap data (pure)
    planner.py              candidate selection + fuel-stop optimizer (pure, no Django)
    baselines.py            the simple "refuel at 25%" rules the optimizer is compared with (pure)
    navigation.py           the plan as Google Maps links and a GPX file (pure)
    warmup.py               start-up work done in the background so the first search is not slow
    trip.py                 orchestrates one request
  management/commands/      build_places, fetch_overture_places, build_station_coords (one-off),
                            load_stations, compare_rules, check_ors
  data/                     fuel-prices.csv, us_places.csv, us_communities.csv,
                            station_geocode_overrides.json, station_coords.csv,
                            overture_places.csv.gz (+ .release)
  templates/                Leaflet map page
  tests/                    247 tests (incl. optimizer vs. brute force and vs. a physical simulation)
.osm_cache/                 gzipped OpenStreetMap download used by build_station_coords (12 MB)
requirements.txt            the app's dependencies (duckdb, for fetch_overture_places only, is optional)
```
