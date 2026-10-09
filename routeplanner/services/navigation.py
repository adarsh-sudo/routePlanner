"""Hand the plan to a navigation app: Google Maps links and a GPX file. Pure: no Django, no network.

This app plans *where to fill up*; it does not navigate. These helpers carry that plan, in order,
into software that does:

* ``google_maps_links``: Google Maps "directions" URLs with the fuel stops as waypoints. Google's
  documented limits are 9 waypoints per link (3 on mobile browsers) and 2,048 characters per URL,
  so a long plan is split into parts that each start where the last ended.
* ``gpx``: a GPX 1.1 file (the standard exchange format for GPS devices and apps) with the stops as
  waypoints and the whole trip as a route, for devices that route with their own, e.g. truck, profile.
  Stops whose position is only a guess (city centre, or a station the map sources dispute) are
  waypoints but not route points.
"""

import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode

GOOGLE_DIRECTIONS = "https://www.google.com/maps/dir/"
MAX_URL_CHARS = 2048
DESKTOP_WAYPOINTS = 9
MOBILE_WAYPOINTS = 3

GPX_NS = "http://www.topografix.com/GPX/1/1"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

LOCATION_NOTES = {
    "site": "Placed at the fuel station.",
    "exit": "Placed at its highway exit; the station is close by.",
    "uncertain": (
        "Two map sources disagree on which station this is, so it is left out of the route: "
        "check the name when you arrive."
    ),
    "city": "Position approximate (city centre), so it is left out of the route: find it by name and address.",
}


def _stop_target(stop):
    """How a stop is named to Google: exact coordinates when we know where it is, else name and town.

    A stop still at its city centre, or one whose station the two map sources dispute, has a guessed
    position, but Google knows the business itself.
    """
    if stop["location"] in ("site", "exit"):
        return f"{stop['lat']:.5f},{stop['lon']:.5f}"
    return f"{stop['name']}, {stop['city']}, {stop['state']}".replace("|", " ")


def _stop_label(stop):
    return f"stop {stop['order']}"


def _url(origin, waypoints, destination):
    params = {"api": 1, "origin": origin, "destination": destination, "travelmode": "driving"}
    if waypoints:
        params["waypoints"] = "|".join(waypoints)
    params["dir_action"] = "navigate"
    return GOOGLE_DIRECTIONS + "?" + urlencode(params, quote_via=quote, safe=",")


def google_maps_links(start_text, finish_text, stops, max_waypoints):
    """Directions links, each ``{"label", "url"}``, covering start -> every stop in order -> finish.

    Each link has at most ``max_waypoints`` waypoints and fits a 2,048 character URL; when the plan
    needs more than one, link n starts at the destination of link n-1.
    """
    targets = [_stop_target(s) for s in stops] + [finish_text]
    names = [_stop_label(s) for s in stops] + ["the finish"]
    for waypoints in range(max_waypoints, 0, -1):  # shrink if a URL would be too long
        size = waypoints + 1  # targets per link: the waypoints and the destination
        chunks = [(i, min(i + size, len(targets))) for i in range(0, len(targets), size)]
        links = []
        for part, (lo, hi) in enumerate(chunks, 1):
            origin = start_text if lo == 0 else targets[lo - 1]
            come_from = "the start" if lo == 0 else names[lo - 1]
            label = "Open in Google Maps" if len(chunks) == 1 else f"Part {part} of {len(chunks)}: {come_from} to {names[hi - 1]}"
            links.append({"label": label, "url": _url(origin, targets[lo:hi - 1], targets[hi - 1])})
        if all(len(link["url"]) <= MAX_URL_CHARS for link in links):
            return links
    return links


def _point(parent, tag, lat, lon, name, desc=None):
    el = ET.SubElement(parent, f"{{{GPX_NS}}}{tag}", lat=f"{lat:.6f}", lon=f"{lon:.6f}")
    ET.SubElement(el, f"{{{GPX_NS}}}name").text = name
    if desc:
        ET.SubElement(el, f"{{{GPX_NS}}}desc").text = desc
    return el


def gpx(payload):
    """GPX 1.1 (bytes) for a ``plan_trip`` payload: each stop as a waypoint, and start -> stops -> finish as a route."""
    start, finish, stops = payload["start"], payload["finish"], payload["fuel_stops"]
    S = payload["summary"]
    title = f"FuelRoute: {start['label']} to {finish['label']}"
    ET.register_namespace("", GPX_NS)
    ET.register_namespace("xsi", XSI_NS)
    root = ET.Element(
        f"{{{GPX_NS}}}gpx",
        {
            "version": "1.1",
            "creator": "FuelRoute",
            f"{{{XSI_NS}}}schemaLocation": f"{GPX_NS} http://www.topografix.com/GPX/1/1/gpx.xsd",
        },
    )
    meta = ET.SubElement(root, f"{{{GPX_NS}}}metadata")
    ET.SubElement(meta, f"{{{GPX_NS}}}name").text = title
    ET.SubElement(meta, f"{{{GPX_NS}}}desc").text = (
        f"{payload['route']['distance_miles']:.0f} miles, {S['num_fuel_stops']} fuel stops, "
        f"${S['total_fuel_cost']:,.2f} of fuel at {S['range_miles']:.0f} mi range and {S['mpg']:.0f} mpg. "
        "Stop positions come from OpenStreetMap (c) OpenStreetMap contributors, ODbL, and Overture Maps, "
        "CDLA-Permissive-2.0; check them against the signs, and keep a fuel reserve."
    )

    def stop_desc(s):
        return (
            f"{s['address']}, {s['city']}, {s['state']}. ${s['price_per_gallon']:.3f}/gal: "
            f"buy {s['gallons']:.1f} gal for ${s['cost']:.2f}. {LOCATION_NOTES[s['location']]}"
        )

    for s in stops:
        _point(root, "wpt", s["lat"], s["lon"], f"{s['order']}. {s['name']}", stop_desc(s))
    rte = ET.SubElement(root, f"{{{GPX_NS}}}rte")
    ET.SubElement(rte, f"{{{GPX_NS}}}name").text = title
    _point(rte, "rtept", start["lat"], start["lon"], f"Start: {start['label']}")
    for s in stops:
        # A guessed position (city centre, or one of two disputed stations) must not become a via point:
        # a GPS would drive to the guess.
        if s["location"] not in ("city", "uncertain"):
            _point(rte, "rtept", s["lat"], s["lon"], f"{s['order']}. {s['name']}", stop_desc(s))
    _point(rte, "rtept", finish["lat"], finish["lon"], f"Finish: {finish['label']}")
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
