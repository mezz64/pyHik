"""Tests for one-time Digest authentication recovery on stream requests."""

import threading
import unittest
from unittest.mock import MagicMock

import requests
from requests.auth import HTTPDigestAuth

from pyhik.hikvision import HikCamera


def camera_for_digest_test(auth):
    camera = HikCamera.__new__(HikCamera)
    camera.name = 'Test camera'
    camera.cam_id = 'test-id'
    camera.root_url = 'http://localhost:80'
    camera.usr = 'test-user'
    camera.pwd = 'test-password'
    camera.hik_request_stream = MagicMock()
    camera.hik_request_stream.auth = auth
    camera.watchdog = MagicMock()
    camera._stream_connected = False
    camera._updateCallbacks = []
    camera.event_states = {}
    camera.update_stale = MagicMock()
    camera.process_stream = MagicMock()
    return camera


def response(status, kill_event=None):
    result = MagicMock(status_code=status)
    result.headers = {}

    def lines():
        if kill_event is not None:
            kill_event.set()
        return iter(())

    result.iter_lines.side_effect = lines
    return result


def stop_on_first_backoff():
    kill = MagicMock()
    kill.is_set.return_value = False
    kill.wait.return_value = True
    return kill


class AlertStreamDigestTests(unittest.TestCase):
    def test_401_retries_once_with_fresh_digest_auth(self):
        old_auth = HTTPDigestAuth('test-user', 'test-password')
        camera = camera_for_digest_test(old_auth)
        kill = threading.Event()
        unauthorized = response(requests.codes.unauthorized)
        success = response(requests.codes.ok, kill)
        camera.hik_request_stream.get.side_effect = [unauthorized, success]

        camera.alert_stream(threading.Event(), kill)

        self.assertEqual(camera.hik_request_stream.get.call_count, 2)
        unauthorized.close.assert_called_once()
        self.assertIsInstance(camera.hik_request_stream.auth, HTTPDigestAuth)
        self.assertIsNot(camera.hik_request_stream.auth, old_auth)
        self.assertEqual(camera.hik_request_stream.auth.username, 'test-user')

    def test_second_401_is_not_retried_again(self):
        camera = camera_for_digest_test(
            HTTPDigestAuth('test-user', 'test-password'))
        camera.hik_request_stream.get.side_effect = [
            response(requests.codes.unauthorized),
            response(requests.codes.unauthorized)]
        with self.assertLogs('pyhik.hikvision', level='WARNING'):
            camera.alert_stream(threading.Event(), stop_on_first_backoff())
        self.assertEqual(camera.hik_request_stream.get.call_count, 2)

    def test_basic_auth_does_not_trigger_digest_retry(self):
        camera = camera_for_digest_test(('test-user', 'test-password'))
        camera.hik_request_stream.get.return_value = response(
            requests.codes.unauthorized)
        with self.assertLogs('pyhik.hikvision', level='WARNING'):
            camera.alert_stream(threading.Event(), stop_on_first_backoff())
        camera.hik_request_stream.get.assert_called_once()

    def test_success_does_not_replace_digest_auth(self):
        old_auth = HTTPDigestAuth('test-user', 'test-password')
        camera = camera_for_digest_test(old_auth)
        kill = threading.Event()
        camera.hik_request_stream.get.return_value = response(
            requests.codes.ok, kill)
        camera.alert_stream(threading.Event(), kill)
        camera.hik_request_stream.get.assert_called_once()
        self.assertIs(camera.hik_request_stream.auth, old_auth)


if __name__ == '__main__':
    unittest.main()
