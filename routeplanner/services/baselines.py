"""The simple "refuel when the tank gets low" rules that the optimal plan is compared against.

Pure functions: no Django, no network. ``compare_rules`` prints the comparison in the README.

The rule
--------
The vehicle starts full. Once the tank has dropped to ``trigger_level`` miles of range it must
refuel: it looks at the stations from that point up to ``window_miles`` further on, takes the
cheapest, and fills the tank there. If there is none in that window it takes the nearest station it
can still reach. It does not stop if the destination is already within reach of what is in the
tank. Two settings are compared in the README:

  25% used  trigger_level = 75% of the range (refuel early, after a quarter of the tank is used)
  25% left  trigger_level = 25% of the range (refuel late, with a quarter of the tank left)

Costs use the same model as the optimizer: every mile is paid for at the price it was bought at
(first in, first out), fuel left in the tank at the destination is not charged, and a detour to a
station ``d`` miles off the route burns ``2d`` miles of range (``d`` out, ``d`` back).
"""

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuleResult:
    cost: float  # fuel actually burnt, at the prices it was bought at
    stops: int
    gallons: float  # fuel burnt, including detours


def refuel_rule(
    candidates, total_miles, start_price, trigger_level, window_miles=50.0, range_miles=500.0, mpg=10.0
):
    """Run the threshold rule over ``candidates`` (sorted by mile). None if it runs dry on the way."""
    lots = deque([[start_price, range_miles]])  # (price, miles of range), oldest first
    spent = 0.0  # dollars of fuel burnt so far
    burnt = 0.0  # miles of range burnt so far
    stops = 0
    mile = 0.0  # current route position
    level = range_miles  # miles of range in the tank at ``mile``
    last = -1  # index of the last station used

    def burn(miles):
        nonlocal spent, burnt
        burnt += miles
        while miles > 1e-9:
            price, have = lots[0]
            take = min(miles, have)
            spent += take / mpg * price
            miles -= take
            if take >= have - 1e-9:
                lots.popleft()
            else:
                lots[0][1] = have - take

    while True:
        if total_miles - mile <= level + 1e-9:  # the destination is within reach: no more stops
            burn(total_miles - mile)
            return RuleResult(spent, stops, burnt / mpg)
        trigger = mile + max(0.0, level - trigger_level)  # where the tank hits the trigger level
        ahead = [
            (i, c) for i, c in enumerate(candidates)
            if i > last and c.mile >= trigger - 1e-9
            and level - (c.mile - mile) >= c.off  # enough range left to reach its pumps
        ]
        if not ahead:
            return None
        window = [(i, c) for i, c in ahead if c.mile <= trigger + window_miles]
        if window:
            i, c = min(window, key=lambda ic: (ic[1].station.price, ic[1].mile))
        else:
            i, c = min(ahead, key=lambda ic: ic[1].mile)
        burn(c.mile - mile + c.off)  # drive there, and out to the pumps
        stops += 1
        fill = range_miles - lots_total(lots)
        lots.append([c.station.price, fill])  # fill the tank
        burn(c.off)  # and back to the route
        mile, level, last = c.mile, lots_total(lots), i


def lots_total(lots):
    return sum(have for _, have in lots)
