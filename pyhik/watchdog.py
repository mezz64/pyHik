"""
pyhik.watchdog
~~~~~~~~~~~~~~~~~~~~
Watchdog class
Copyright (c) 2017-2018 John Mihalic <https://github.com/mezz64>
Licensed under the MIT license.
"""

from threading import Timer


class Watchdog(object):
    """ Watchdog timer class. """

    def __init__(self, timeout, handler):
        """ Initialize watchdog variables. """
        self.time = timeout
        self.handler = handler
        return

    def start(self):
        """ Starts the watchdog timer. """
        self._timer = Timer(self.time, self.handler)
        self._timer.daemon = True
        self._timer.start()
        return

    def pet(self):
        """ Reset watchdog timer. """
        self.stop()
        self.start()
        return

    def stop(self):
        """ Stops the watchdog timer. """
        self._timer.cancel()
