"""Do the one-off work the first request would otherwise wait for. Started by ``config/wsgi.py``.

After a server restart the first search pays to load the station table (~40 ms), build the city
index (~0.2 s), build the index of place names given without a state (~0.5 s, ~65 MB) and open a
connection to the routing server (~0.8 s). This does all of it in a background thread at start-up,
so the first user does not. Set ``FUEL_WARM_ON_START=0`` to skip it (e.g. to save the memory).
"""

import logging
import os
import threading

from . import places, routing
from .stations import get_station_index

log = logging.getLogger(__name__)


def warm_up():
    """Each step is best effort: a failure here must never stop the server starting."""
    from django.db import connection

    steps = (
        ("station table", get_station_index),
        ("city index", lambda: places.lookup_city("Denver", "CO")),
        ("place-name index", lambda: places.resolve_place("Chicago")),
        ("routing connection", routing.warm),
    )
    try:
        for name, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - see docstring
                log.warning("warm-up: %s skipped (%s)", name, exc)
    finally:
        connection.close()  # this thread opened its own database connection


def warm_in_background():
    """Start ``warm_up`` in a daemon thread unless switched off. Returns the thread, or None."""
    if os.environ.get("FUEL_WARM_ON_START", "1") != "1":
        return None
    thread = threading.Thread(target=warm_up, name="fuelroute-warmup", daemon=True)
    thread.start()
    return thread
