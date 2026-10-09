# FuelRoute: project notes

UI work follows [frontend.md](frontend.md). The brief wins where it is specific; this file records the
design decisions that came out of following it, so later work keeps them.

## Design record (map page: `routeplanner/templates/routeplanner/map.html`)

- **Subject / audience / job:** US fuel-stop planner (500 mi range, 10 mpg). Drivers and trip planners,
  desk first and phone second. Job: type two places, read the total fuel cost and where to fill up.
- **Colour:** "Shade + Pastel" (frontend.md 3.3). Dark tinted ink supplies structure; powder pastels
  *categorise*, they do not decorate. Brand is highway blue `#2e5e94`; powder blue `#d6e7f6` marks start,
  selected and hovered rows. Price tiers: mint = lowest, butter = middle, blush = highest; lavender =
  arrival. Every pastel has a matching ink for text and a 2px ink border on pins and chips (the fill alone
  is never the signal). All pairs were computed for WCAG AA in light and dark.
- **Type:** Overpass (open-source face based on US highway-sign lettering), weights 400/700/800, tabular
  figures for every number. Fallback is Arial at `size-adjust: 98%` (measured against Overpass).
- **Memorable thing:** the tank-level chart (fuel in the tank along the route). Everything else stays quiet.
- **Motion:** one orchestrated moment on results (route draws, chart grows, pins drop in as the route
  passes them). Everything else is a response to the user. Reduced motion is respected.
- **Radius / elevation:** 8px inputs and buttons, 16px popups, full for pins and pills; the planner is
  docked with no radius. Two shadows only: raised (pins, zoom) and overlay (popups).
- **Copy:** sentence case; the button says what it does ("Find fuel stops"); the headline states the
  answer; errors say what happened and how to fix it (hint per HTTP status in `views.ERROR_HINTS`).
- **Honest numbers:** a stop's detour shows as "1.5 mi detour" when measured by road and "about 0.8 mi
  detour" when only estimated. A popup says "Position approximate: city centre" for a stop still at its
  city centre, "Pin is at the highway exit; the station is close by" for one placed at its exit, and "Two
  map sources disagree on which station this is: check the name when you arrive" for an `uncertain` one (a
  pin that is not surely on the station must never look like it is). The footer credits OpenStreetMap and
  Overture.
  The tank chart subtracts each detour (`detour_miles`) so the line never overshoots the real level.
- **Ambiguous names:** a name typed without a state means the most populous place of that name (never the
  largest by land area), and the note under the form says which was chosen and offers the others as one-tap
  links (`Meant a different one? Miami, OH · ...`) so a wrong guess is one tap to fix, not a retype.
- **Truck routes:** this is a truck planner. An `ORS_API_KEY` switches the engine to OpenRouteService
  (`driving-hgv`, sizes in `settings.TRUCK`), which plans the route and the detours for a truck. With no key the
  app still runs on OSRM (car routes) so it works out of the box, and the page shows a note, `routing.note`
  ("This is a car route, not a truck route..."), so a car route is never passed off as a truck's;
  `ROUTING_ENGINE=osrm` forces it. For a truck route the page shows "Routed for a truck
  13 ft 6 in high, 80,000 lb." under the summary and credits "openrouteservice.org by HeiGIT" in the footer
  (from `payload.routing.credit`); the hand-off note still says Google Maps plans a car route. The key is sent
  in a header only and never reaches the page, a URL or an error message; it lives in the git-ignored `.env`. Confirmed against the live service on
  2026-10-09 (route, matrix, truck drive times); a real low bridge or weight limit is not tested (see README
  "Truck routes").
- **Hand-off:** the page plans, it does not navigate, and says so. "Take this plan with you" sits after the
  stop list: one outlined row per Google Maps link (a phone gets the 3-waypoint parts) and a GPX download.
  Links are built with DOM calls, not string HTML.
- **Phones (under 900 px):** the map sits above the planner (`column-reverse`) and is at least 22rem tall. A
  popup opens above its pin, so it must fit that short map: `autoPan` is on (touch: padded 60px right, 100px
  below so the pin stays clear of the credit line), the popup opens on the fly-to's `moveend`, and a tall
  popup scrolls. On touch the zoom buttons go top-right (a bottom-right stack pushed the credit line over the
  pins), and are hidden under 360px. Tapping a pin must not scroll the page (the list is far below on a
  phone; the scroll-to-row only runs in the side-panel layout). Tap targets stay 44px or more.
- **First load (Lighthouse):** the page builds its result in JS, so the server sends `#results` `hidden` behind
  an outline (`#results-sk`, the same `.sk` shapes as the submit-time skeleton), and the script swaps the two in
  one task; without that the fixed headings painted first and jumped down ~1,700 px (CLS 0.20 desktop, 0.10
  phone; now 0.02 and 0.002). Leaflet's script is `defer` in the head and the page script runs on
  `DOMContentLoaded`; the Google Fonts stylesheet loads with `media="print" onload` (its fallback face is
  size-matched, so the swap is quiet); the tile server is preconnected. If Leaflet fails to load, the outline is
  replaced by an alert. Left alone on purpose: Leaflet's stylesheet blocks first paint (needed so the map is
  laid out), the `stroke-dashoffset` route-draw cannot be composited, and tile cache headers / HTTP version /
  256px tiles belong to Esri.
- **Basemap:** Esri gray canvas (keyless) with a powder wash. OSM's tile server blocks requests with no
  Referer and CARTO's now require a key, so neither is used.
- **Not added on purpose:** inputs beyond start and finish (the brief takes two). See the README upgrade plan.

## Gotchas

- The map template is a Django template: avoid `{{`, `{%` and `{#` in the CSS or JS.
- Settings can come from a private `.env` file (`config/envfile.py`; copy `.env.example`). Real environment variables win,
  blank values are ignored, and `manage.py test` ignores the default `.env` so a developer's own key or engine cannot
  change or block the tests. Never read or print the real `.env`: it holds the user's API key.
- Editing `config/settings.py` (or any module imported at start-up) while `runserver` is running: the auto-reloader
  restarts on every save, and a crash at import (a NameError between two edits, say) makes `runserver` exit for good.
  Keep each save a valid file: change it in one edit, not in two halves.
- Tests pin `ROUTING_ENGINE="osrm"` (and the ORS ones `"ors"`) with `override_settings`, so an engine set in the shell
  cannot change them.
- Leaflet is deferred: do not call `L` from a script that runs before `DOMContentLoaded`.
- Tests assert `name="start" value="..."` (attribute order), `role="alert"` on errors and `id="trip-data"`.
