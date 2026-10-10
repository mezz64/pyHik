"""Regression tests for reconnecting finite/closed alert streams."""

import threading
import unittest
from unittest.mock import MagicMock, call, patch

import requests

from pyhik.hikvision import HikCamera


def camera_for_stream_test():
    """Build a camera without network setup or starting a real thread."""
    camera = HikCamera.__new__(HikCamera)
    camera.name = "Test camera"
    camera.cam_id = "test-id"
    camera.root_url = "http://localhost:80"
    camera.hik_request_stream = MagicMock()
    camera.watchdog = MagicMock()
    camera._stream_connected = False
    camera._updateCallbacks = []
    camera.event_states = {}
    camera.update_stale = MagicMock()
    camera.process_stream = MagicMock()
    return camera


def response_with_lines(headers=None, lines=()):
    response = MagicMock(status_code=requests.codes.ok)
    response.headers = {} if headers is None else headers
    response.iter_lines.side_effect = lambda: iter(lines)
    return response


def stop_after_retries(retries):
    """Allow `retries` full backoff cycles, then stop at the next wait."""
    kill = MagicMock()
    kill.is_set.return_value = False
    kill.wait.side_effect = [False] * (2 * retries) + [True]
    return kill


class AlertStreamReconnectTests(unittest.TestCase):
    def test_finite_empty_response_has_no_availability_flapping(self):
        camera = camera_for_stream_test()
        response = response_with_lines(
            {'Content-Length': '40'},
            [b'<?xml version="1.0" encoding="UTF-8"?>'])
        camera.hik_request_stream.get.return_value = response
        with patch.object(camera, '_set_stream_connected',
                          wraps=camera._set_stream_connected) as connected:
            with self.assertLogs('pyhik.hikvision', level='WARNING') as logs:
                camera.alert_stream(threading.Event(), stop_after_retries(0))
        self.assertTrue(any('Event stream ended unexpectedly' in msg
                            for msg in logs.output))
        self.assertFalse(camera.stream_connected)
        self.assertNotIn(call(True), connected.call_args_list)
        response.close.assert_called_once()

    def test_immediate_eof_uses_increasing_interruptible_backoff(self):
        camera = camera_for_stream_test()
        camera.hik_request_stream.get.return_value = response_with_lines(
            {'Content-Length': '40'})
        kill = stop_after_retries(2)
        with self.assertLogs('pyhik.hikvision', level='WARNING') as logs:
            camera.alert_stream(threading.Event(), kill)
        self.assertEqual(camera.hik_request_stream.get.call_count, 3)
        self.assertEqual([call(5), call(5), call(5), call(10), call(5)],
                         kill.wait.call_args_list)
        self.assertTrue(any('count=3' in message for message in logs.output))

    def test_backoff_is_capped_at_five_minutes(self):
        camera = camera_for_stream_test()
        camera.hik_request_stream.get.return_value = response_with_lines(
            {'Content-Length': '40'})
        kill = stop_after_retries(60)
        with self.assertLogs('pyhik.hikvision', level='WARNING'):
            camera.alert_stream(threading.Event(), kill)
        intervals = [c.args[0] for c in kill.wait.call_args_list]
        self.assertEqual(intervals[1], 5)
        self.assertEqual(intervals[3], 10)
        self.assertEqual(intervals[117], 295)  # attempt 59
        self.assertEqual(intervals[119], 295)  # attempt 60
        self.assertLessEqual(max(intervals), 295)

    def test_long_lived_connection_resets_failure_count(self):
        camera = camera_for_stream_test()
        clock = [0.0]
        short = response_with_lines({'Content-Length': '40'})
        long = response_with_lines({'Content-Length': '40'})

        def long_response_lines():
            clock[0] += 31
            return iter(())

        long.iter_lines.side_effect = long_response_lines
        camera.hik_request_stream.get.side_effect = [short, short, long, short]
        with patch('pyhik.hikvision.time.monotonic',
                   side_effect=lambda: clock[0]):
            with self.assertLogs('pyhik.hikvision', level='WARNING') as logs:
                camera.alert_stream(threading.Event(), stop_after_retries(3))
        counts = [message.split('count=')[1].split(')')[0]
                  for message in logs.output]
        self.assertEqual(counts, ['1', '2', '1', '2'])

    def test_indefinite_stream_can_connect_without_first_event(self):
        camera = camera_for_stream_test()
        kill = threading.Event()
        response = response_with_lines()

        with patch('pyhik.hikvision.threading.Timer') as timer:
            def lines():
                self.assertFalse(camera.stream_connected)
                # Simulate the one-second confirmation of a quiet stream.
                timer.call_args.args[1]()
                self.assertTrue(camera.stream_connected)
                kill.set()
                yield b'--boundary'

            response.iter_lines.side_effect = lines
            camera.hik_request_stream.get.return_value = response
            with patch.object(camera, '_set_stream_connected',
                              wraps=camera._set_stream_connected) as connected:
                camera.alert_stream(threading.Event(), kill)

        self.assertEqual(connected.call_args_list,
                         [call(True), call(False)])
        timer.return_value.cancel.assert_called()
        camera.hik_request_stream.get.assert_called_once()

    def test_empty_stream_without_content_length_never_becomes_available(self):
        camera = camera_for_stream_test()
        response = response_with_lines(headers={})
        camera.hik_request_stream.get.return_value = response

        with patch('pyhik.hikvision.threading.Timer') as timer:
            with patch.object(camera, '_set_stream_connected',
                              wraps=camera._set_stream_connected) as connected:
                with self.assertLogs('pyhik.hikvision', level='WARNING'):
                    camera.alert_stream(threading.Event(),
                                        stop_after_retries(0))
                # Even a callback already queued when the timer is canceled
                # cannot publish a stale "connected" state after EOF.
                timer.call_args.args[1]()

        self.assertNotIn(call(True), connected.call_args_list)
        timer.return_value.cancel.assert_called()

    def test_content_length_stream_can_become_available_while_quiet(self):
        camera = camera_for_stream_test()
        kill = threading.Event()
        response = response_with_lines({'Content-Length': '4096'})

        with patch('pyhik.hikvision.threading.Timer') as timer:
            def lines():
                self.assertFalse(camera.stream_connected)
                timer.call_args.args[1]()
                self.assertTrue(camera.stream_connected)
                kill.set()
                yield b''

            response.iter_lines.side_effect = lines
            camera.hik_request_stream.get.return_value = response
            camera.alert_stream(threading.Event(), kill)

        timer.return_value.cancel.assert_called()
        camera.hik_request_stream.get.assert_called_once()
        self.assertFalse(camera.stream_connected)

    def test_content_length_valid_alert_connects_immediately(self):
        camera = camera_for_stream_test()
        kill = threading.Event()
        response = response_with_lines({'Content-Length': '4096'}, [
            b'<EventNotificationAlert>',
            b'<eventType>VMD</eventType>',
            b'</EventNotificationAlert>',
        ])
        camera.hik_request_stream.get.return_value = response

        def on_event(_):
            self.assertTrue(camera.stream_connected)
            kill.set()

        camera.process_stream.side_effect = on_event
        with patch('pyhik.hikvision.threading.Timer') as timer:
            with patch.object(camera, '_set_stream_connected',
                              wraps=camera._set_stream_connected) as connected:
                camera.alert_stream(threading.Event(), kill)

        camera.process_stream.assert_called_once()
        self.assertEqual(connected.call_args_list,
                         [call(True), call(False)])
        timer.return_value.cancel.assert_called()


if __name__ == '__main__':
    unittest.main()
