"""
pyhik.watchdog
~~~~~~~~~~~~~~~~~~~~
Watchdog class
Copyright (c) 2017-2018 John Mihalic <https://github.com/mezz64>
Licensed under the MIT license.
"""

from threading import RLock, Timer


class Watchdog(object):
    """ Watchdog timer class. """

    def __init__(self, timeout, handler):
        """ Initialize watchdog variables. """
        self.time = timeout
        self.handler = handler
        self._timer = None
        # Identifies the current timer, so an expiry from one that was
        # replaced or stopped can tell it is stale.
        self._token = None
        # start(), pet() and stop() run on the stream thread while expiries
        # run on timer threads. Reentrant because the handler may call stop().
        self._lock = RLock()
        return

    def start(self):
        """ Starts the watchdog timer, replacing any timer already running. """
        with self._lock:
            # Replacing a running timer without cancelling it leaves it
            # orphaned: it can no longer be pet or stopped, and fires anyway.
            self._cancel()
            token = object()
            self._token = token
            self._timer = Timer(self.time, self._expire, args=(token,))
            self._timer.daemon = True
            self._timer.start()
        return

    def pet(self):
        """ Reset watchdog timer. """
        self.start()
        return

    def stop(self):
        """ Stops the watchdog timer. """
        with self._lock:
            self._cancel()
        return

    def _cancel(self):
        """ Cancel the current timer. Callers hold the lock. """
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._token = None

    def _expire(self, token):
        """ Call the handler, unless this timer was replaced or stopped. """
        with self._lock:
            # cancel() cannot stop a timer that has already fired, so a timer
            # replaced or stopped at the moment it expired still gets here.
            if token is not self._token:
                return
            self._timer = None
            self._token = None
            self.handler()
