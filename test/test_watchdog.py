"""Tests for the watchdog timer."""

import threading
import time
import unittest

from pyhik.watchdog import Watchdog

# Short enough to keep the tests fast, long enough for a loaded CI runner to
# get through a few calls before it fires.
TIMEOUT = 0.2


class WatchdogTestCase(unittest.TestCase):
    def setUp(self):
        self.fired = threading.Event()
        self.calls = []

        def handler():
            self.calls.append(time.monotonic())
            self.fired.set()

        self.watchdog = Watchdog(TIMEOUT, handler)
        self.addCleanup(self.watchdog.stop)

    def test_fires_after_timeout(self):
        self.watchdog.start()
        self.assertTrue(self.fired.wait(TIMEOUT * 10))
        self.assertEqual(len(self.calls), 1)

    def test_restart_replaces_the_running_timer(self):
        """Starting again must cancel the running timer. Otherwise it is
        orphaned, can no longer be pet or stopped, and fires anyway (#149)."""
        self.watchdog.start()
        self.watchdog.start()
        self.assertTrue(self.fired.wait(TIMEOUT * 10))
        time.sleep(TIMEOUT * 2)
        self.assertEqual(len(self.calls), 1)

    def test_stop_after_restart_cancels_every_timer(self):
        self.watchdog.start()
        self.watchdog.start()
        self.watchdog.stop()
        time.sleep(TIMEOUT * 3)
        self.assertEqual(self.calls, [])

    def test_pet_postpones_expiry(self):
        self.watchdog.start()
        for _ in range(3):
            time.sleep(TIMEOUT / 4)
            self.watchdog.pet()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.fired.wait(TIMEOUT * 10))
        self.assertEqual(len(self.calls), 1)

    def test_expiry_of_a_replaced_timer_is_ignored(self):
        """cancel() cannot stop a timer that has already fired, so an expiry
        can still arrive from a timer that was just replaced."""
        self.watchdog.start()
        stale = self.watchdog._token
        self.watchdog.start()
        self.watchdog._expire(stale)
        self.assertEqual(self.calls, [])

    def test_expiry_after_stop_is_ignored(self):
        self.watchdog.start()
        stale = self.watchdog._token
        self.watchdog.stop()
        self.watchdog._expire(stale)
        self.assertEqual(self.calls, [])

    def test_handler_may_stop_the_watchdog(self):
        """HikCamera.watchdog_handler calls stop() from the timer thread."""
        returned = threading.Event()

        def handler():
            self.watchdog.stop()
            returned.set()

        self.watchdog.handler = handler
        self.watchdog.start()
        self.assertTrue(returned.wait(TIMEOUT * 10))

    def test_slow_handler_does_not_block_pet_and_stop(self):
        """Watchdog is public, so a handler may take its time. The stream
        thread pets and stops the watchdog and must not wait on it."""
        entered = threading.Event()
        release = threading.Event()

        def handler():
            entered.set()
            release.wait(TIMEOUT * 20)

        self.addCleanup(release.set)
        self.watchdog.handler = handler
        self.watchdog.start()
        self.assertTrue(entered.wait(TIMEOUT * 10))
        returned = threading.Event()

        def pet_then_stop():
            self.watchdog.pet()
            self.watchdog.stop()
            returned.set()

        threading.Thread(target=pet_then_stop, daemon=True).start()
        self.assertTrue(returned.wait(TIMEOUT * 5))


if __name__ == '__main__':
    unittest.main()
