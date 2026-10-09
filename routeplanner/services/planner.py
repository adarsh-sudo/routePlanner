"""Pick fuel stops along a route. Pure functions: no Django, no network.

Model
-----
The vehicle has a ``range_miles`` tank (``range_miles / mpg`` gallons) and burns
fuel linearly with distance. Stations along the route are nodes at a mile marker
with a $/gal price; any amount up to the tank size can be bought. The trip starts
with a full tank which is priced at ``start_price`` (the cheapest station near the
origin) - modelled as a free-to-visit virtual station at mile 0 selling at that
price, so the fuel actually used from the first tank is charged at that price.

Detours
-------
A station ``d`` miles off the route costs a round trip of ``2d`` miles. That fuel is really
burnt, so it has to be bought on top of what the route itself needs: ``2d / mpg`` extra gallons
per detour, so total gallons = distance / mpg + the detours. The way out is driven on fuel
already in the tank (bought earlier, at the earlier price), the way back on fuel bought at the
station; each gallon is priced where it was bought. The detour also limits the fill: the vehicle
needs ``d`` miles of range to reach the pumps, and after filling up it rejoins the route ``d``
miles short of a full tank. Stations are not collapsed by price alone for the same reason: a
pricier station that is closer to the road can be the better buy (see ``find_candidates``).

Objective
---------
Minimise  fuel cost + ``stop_penalty`` x (number of en-route stops).

The penalty is the dollar value of the time/hassle of a stop. Without it the
textbook optimum is to hop between stations buying a gallon or two to chase
fractions of a cent (17 stops for LA -> NYC), which no driver would do. With it,
a stop is only made when it saves more than ``stop_penalty`` in fuel. Set it to
0 for the pure cheapest-fuel plan. The reported cost is fuel only.

Method
------
Exact dynamic programme over (station, fuel on arrival). Distances are rounded
to whole miles and fuel is tracked in miles of range (0..range_miles), so the
state is a vector of ``range_miles + 1`` costs per station and each station is a
handful of vectorised numpy operations:

    after_stop[f'] = penalty + f' * c + min_{f <= f'} (arrive[f] - f * c)
    depart[f']     = min(arrive[f'], after_stop[f'])        # c = price / mpg

Driving to the next station shifts the vector down by the gap in miles. A station with a detour
only changes the stop step: a stop can start only from arrival levels >= d, may buy back up to
``2d`` miles of range more than it adds to the level, and must leave at a level <= range - d.
"""

import math
from dataclasses import dataclass

import numpy as np

MIN_STOP_GALLONS = 0.01  # a visited station where nothing is bought is not a stop


class NoFeasiblePlan(RuntimeError):
    """The trip can't be completed: a gap between stations exceeds the vehicle's range."""

    def __init__(self, mile):
        super().__init__(
            f"No fuel station within range after mile {mile:.0f} of the route; "
            "the vehicle cannot complete this trip."
        )
        self.mile = mile


@dataclass(frozen=True, slots=True)
class Candidate:
    station: object  # StationRec
    mile: float  # distance along the route
    off_route: float  # straight-line miles from the route
    route_idx: int = -1  # index of the route point nearest the station
    detour: float | None = None  # real driven round-trip miles off the route, once known

    @property
    def off(self):
        """One-way miles the planner charges for visiting: half the driven round trip, else the straight line."""
        return self.off_route if self.detour is None else self.detour / 2


@dataclass(frozen=True, slots=True)
class Purchase:
    candidate: Candidate | None  # None for the starting tank
    price: float
    gallons: float  # everything bought here (includes the fuel for the way back from the pumps)
    detour_gallons: float = 0.0  # fuel burnt on the round trip to these pumps, 2 x off-route miles / mpg

    @property
    def cost(self):
        return self.gallons * self.price


@dataclass(frozen=True, slots=True)
class Plan:
    start: Purchase  # gallons of the starting tank actually used, at the start price
    stops: list  # list[Purchase], in route order
    total_gallons: float  # distance / mpg + detour_gallons
    total_cost: float  # fuel only (stop penalty excluded)
    detour_gallons: float = 0.0  # fuel burnt driving off the route to the chosen stations


def find_candidates(route, stations, radius_miles, total_miles):
    """Stations within ``radius_miles`` of the route, as Candidates sorted by mile.

    Mile markers are rescaled so the route ends exactly at ``total_miles``
    (the routing service's own distance). Among stations sharing a whole-mile
    marker, one is dropped only if another is at least as cheap AND at least as
    close to the road, because then it can never be the better stop. A cheaper
    station farther off the route and a pricier one right on it both survive;
    the planner weighs the price against the detour.
    """
    scale = total_miles / route.length_miles if route.length_miles else 1.0
    groups = {}  # whole-mile marker -> candidates there that no other beats on both price and distance
    for s in stations.in_bbox(*route.bbox(radius_miles)):
        hit = route.nearest(s.lat, s.lon, radius_miles)
        if hit is None:
            continue
        idx, dist = hit
        mile = min(route.miles[idx] * scale, total_miles)
        group = groups.setdefault(round(mile), [])
        if any(g.station.price <= s.price and g.off_route <= dist for g in group):
            continue
        group[:] = [g for g in group if not (s.price <= g.station.price and dist <= g.off_route)]
        group.append(Candidate(s, mile, dist, idx))
    return sorted((c for g in groups.values() for c in g), key=lambda c: (c.mile, c.station.price))


def detour_checks(plan, candidates, limit):
    """Which stations to measure by road: the chosen stops, then the likeliest stand-ins.

    The plan was made with straight-line detours; once real driven detours are known it can
    change its mind, so the stops it would swap to are measured in the same request: the
    candidates nearest (by mile marker, then price) to a chosen stop.

    Only stations matched to their real fuel station (``location_source == "site"``) are
    measured. A city centre or a highway-exit point snapped onto a road says nothing true about
    the driven detour (an exit point can land on the wrong carriageway and invent a U-turn), so
    those keep the straight-line estimate.
    """
    exact = [p.candidate for p in plan.stops if p.candidate.station.location_source == "site"]
    if not exact:
        return []
    chosen = [p.candidate for p in plan.stops]
    picked = {c.station.id for c in exact}
    others = [c for c in candidates if c.station.location_source == "site" and c.station.id not in picked]
    others.sort(key=lambda c: (min(abs(c.mile - s.mile) for s in chosen), c.station.price))
    return exact + others[: max(0, limit - len(exact))]


def start_reference(stations, lat, lon, radius_miles, distance_fn):
    """Cheapest station within ``radius_miles`` of the origin; else the nearest one.

    Returns ``(station, miles_from_origin)`` or None if there are no stations.
    """
    best = nearest = None
    for s in stations.stations:
        d = distance_fn(lat, lon, s.lat, s.lon)
        if nearest is None or d < nearest[1]:
            nearest = (s, d)
        if d <= radius_miles and (best is None or s.price < best[0].price):
            best = (s, d)
    return best or nearest


def plan_fuel(candidates, total_miles, start_price, range_miles=500.0, mpg=10.0, stop_penalty=0.0):
    """Choose where and how much to buy. See the module docstring."""
    R = int(round(range_miles))  # tank, in miles of range
    D = int(round(total_miles))
    n = len(candidates)
    miles = [0] + [min(int(round(c.mile)), D) for c in candidates] + [D]
    prices = [start_price] + [c.station.price for c in candidates]
    off = [0.0] + [round(c.off, 3) for c in candidates]  # one-way miles from the route to the pumps

    levels = np.arange(R + 1, dtype=np.float64)
    idx = np.arange(R + 1, dtype=np.int64)
    inf = np.inf

    arrive = np.full(R + 1, inf)
    arrive[0] = 0.0  # we leave the origin with nothing "bought" yet
    stop_from = []  # per node: arrival level that a stop here started from, or -1

    for k in range(n + 1):
        c = prices[k] / mpg  # $ per mile of range
        pen = 0.0 if k == 0 else stop_penalty
        # Detour of ``off`` miles each way, in whole miles of range, always rounded against us:
        reach = math.ceil(off[k])  # range needed on the route to get to the pumps
        shift = math.floor(2 * off[k])  # a stop may leave the level up to this far below the arrival level
        shifted = arrive - levels * c
        shifted[:reach] = inf  # arriving with less than ``reach`` we cannot get to the pumps
        running_min = np.minimum.accumulate(shifted)
        # Index at which the running minimum is attained (for backtracking).
        argmin = np.maximum.accumulate(np.where(shifted == running_min, idx, -1))
        # Leaving at level f' from arrival level f costs c * (f' - f + 2 * off): the 2 * off is
        # the fuel the round trip burns, bought here. f <= f' + shift keeps the purchase >= 0.
        top = np.minimum(idx + shift, R)
        after_stop = running_min[top] + levels * c + 2 * off[k] * c + pen
        after_stop[max(0, R - reach + 1):] = inf  # filled up, the way back leaves it ``reach`` short
        use_stop = after_stop < arrive  # strict: ties prefer not stopping
        depart = np.where(use_stop, after_stop, arrive)
        stop_from.append(np.where(use_stop, argmin[top], -1).astype(np.int32))

        gap = miles[k + 1] - miles[k]
        nxt = np.full(R + 1, inf)
        if gap <= R:
            nxt[: R + 1 - gap] = depart[gap:]
        if not np.isfinite(nxt).any():
            raise NoFeasiblePlan(miles[k])
        arrive = nxt

    a = int(np.argmin(arrive))  # fuel left on arrival (0 unless costs tie)

    # Walk back through the stations to recover where fuel was bought.
    units = {}  # node index -> change in route level (miles of range) at that stop
    for k in range(n, -1, -1):
        depart_level = a + (miles[k + 1] - miles[k])
        came_from = int(stop_from[k][depart_level])
        if came_from >= 0:
            units[k] = depart_level - came_from
            a = came_from
        else:
            a = depart_level

    # What a stop really buys is that change in level plus the fuel its detour burns.
    gallons = {k: (u + 2 * off[k]) / mpg for k, u in units.items()}
    gallons = {k: g for k, g in gallons.items() if g > 0}
    # Rounding the route to whole miles loses < 0.5 mile: give it back to the
    # last purchase so route fuel == distance / mpg exactly.
    if gallons:
        last = max(gallons)
        gallons[last] = max(0.0, gallons[last] + (total_miles - D) / mpg)

    detour = {k: 2 * off[k] / mpg for k in gallons}
    start = Purchase(None, start_price, gallons.get(0, 0.0))
    stops = [
        Purchase(candidates[k - 1], prices[k], g, detour[k])
        for k, g in sorted(gallons.items())
        if k != 0 and g >= MIN_STOP_GALLONS
    ]
    total_gallons = sum(gallons.values())
    total_cost = sum(g * prices[k] for k, g in gallons.items())
    return Plan(start, stops, total_gallons, total_cost, sum(detour.values()))
