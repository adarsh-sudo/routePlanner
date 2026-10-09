"""Keeping the first search fast: connection warm-up, the endpoint behind it, and start-up warming."""

from unittest import mock

import requests
from django.test import SimpleTestCase, TestCase, override_settings

from routeplanner.services import routing, warmup

from .helpers import FakeResponse


@override_settings(ROUTING_ENGINE="osrm", OSRM_MIN_INTERVAL_SECONDS=0)
class ConnectionWarmTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(routing, "_last_call_at", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_opens_a_connection_when_the_server_has_not_been_used(self):
        with mock.patch.object(routing._session, "get", return_value=FakeResponse({})) as get:
            self.assertTrue(routing.warm())
        get.assert_called_once()
        self.assertTrue(get.call_args.args[0].startswith("https://router.project-osrm.org"))

    def test_does_nothing_when_the_connection_was_used_a_moment_ago(self):
        with mock.patch.object(routing, "_last_call_at", routing.time.monotonic() - 5), \
                mock.patch.object(routing._session, "get") as get:
            self.assertFalse(routing.warm())
        get.assert_not_called()

    def test_refreshes_the_connection_before_the_server_closes_it(self):
        old = routing.time.monotonic() - (routing.WARM_IDLE_SECONDS + 5)
        with mock.patch.object(routing, "_last_call_at", old), \
                mock.patch.object(routing._session, "get", return_value=FakeResponse({})) as get:
            self.assertTrue(routing.warm())
        get.assert_called_once()

    def test_a_network_failure_is_swallowed(self):
        with mock.patch.object(routing._session, "get", side_effect=requests.ConnectionError("down")):
            self.assertTrue(routing.warm())  # tried, did not raise: the real call will report a problem

    def test_idle_window_is_shorter_than_the_45_seconds_a_connection_was_seen_to_survive(self):
        self.assertLess(routing.WARM_IDLE_SECONDS, 45)


class WarmEndpointTests(TestCase):
    def test_returns_204_and_warms_the_connection(self):
        with mock.patch.object(routing, "warm") as warm:
            resp = self.client.get("/api/route/warm/")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.content, b"")
        warm.assert_called_once()

    def test_only_get_and_head_are_allowed(self):
        self.assertEqual(self.client.post("/api/route/warm/").status_code, 405)

    def test_the_map_page_points_the_browser_at_it(self):
        resp = self.client.get("/api/route/map/")
        self.assertContains(resp, "/api/route/warm/")


class StartupWarmTests(SimpleTestCase):
    def test_every_step_runs(self):
        with mock.patch.object(warmup, "get_station_index") as stations, \
                mock.patch.object(warmup.places, "lookup_city") as city, \
                mock.patch.object(warmup.places, "resolve_place") as names, \
                mock.patch.object(warmup.routing, "warm") as conn:
            warmup.warm_up()
        for step in (stations, city, names, conn):
            step.assert_called_once()

    def test_a_failing_step_never_stops_the_others_or_the_server(self):
        with mock.patch.object(warmup, "get_station_index", side_effect=RuntimeError("no stations loaded")), \
                mock.patch.object(warmup.places, "lookup_city"), \
                mock.patch.object(warmup.places, "resolve_place"), \
                mock.patch.object(warmup.routing, "warm") as conn, \
                self.assertLogs(warmup.log, "WARNING") as logged:
            warmup.warm_up()  # must not raise
        conn.assert_called_once()
        self.assertIn("station table skipped", logged.output[0])

    def test_runs_in_a_background_thread_and_can_be_switched_off(self):
        with mock.patch.object(warmup, "warm_up") as run, mock.patch.dict("os.environ", {"FUEL_WARM_ON_START": "1"}):
            thread = warmup.warm_in_background()
            thread.join(5)
        run.assert_called_once()
        self.assertTrue(thread.daemon)  # it must never keep the process alive
        with mock.patch.object(warmup, "warm_up") as run, mock.patch.dict("os.environ", {"FUEL_WARM_ON_START": "0"}):
            self.assertIsNone(warmup.warm_in_background())
        run.assert_not_called()
