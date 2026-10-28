"""Tests for a device-specific decision not to start an alert stream."""

import unittest
from unittest.mock import MagicMock, patch

from pyhik.hikvision import HikCamera, _BROKEN_ALERT_STREAM_DEVICES


DEVICE_INFO = {
    'deviceName': 'OUTDOOR STATION',
    'deviceID': '12345678901',
    'model': 'DS-KV8113-WME1(B)',
    'firmwareVersion': 'V2.2.53',
    'firmwareReleasedDate': 'build 230918',
}


def make_camera(**overrides):
    """Initialize using deviceInfo mocks and no device I/O."""
    info = dict(DEVICE_INFO)
    info.update(overrides)
    with patch.object(HikCamera, 'get_device_info', return_value=info), \
         patch.object(HikCamera, 'get_event_triggers', return_value={}), \
         patch.object(HikCamera, 'get_motion_detection', return_value=None):
        return HikCamera(host='example.invalid', usr='test-user', pwd='test-password')


class AlertStreamDeviceFilterTests(unittest.TestCase):
    """A broken stream is skipped without changing other cameras."""

    def test_reported_device_id_matches_known_firmware(self):
        camera = make_camera()
        self.assertIsInstance(_BROKEN_ALERT_STREAM_DEVICES, list)
        self.assertIn(
            (camera.device_model, camera.firmware_version, camera.firmware_build),
            _BROKEN_ALERT_STREAM_DEVICES,
        )

    def test_known_firmware_never_starts_thread_or_event_requests(self):
        camera = make_camera()
        camera.hik_request_stream.get = MagicMock(
            side_effect=AssertionError('unexpected event-stream request')
        )
        camera.start_stream()
        self.assertIsNone(camera.thrd.ident)
        self.assertFalse(camera.stream_connected)
        camera.hik_request_stream.get.assert_not_called()
        camera.disconnect()
        self.assertFalse(camera.kill_thrd.is_set())

    def test_additional_device_can_be_added_without_changing_start_stream(self):
        additional = ('EXAMPLE-INTERCOM', 'V9.9.9', 'build 999999')
        with patch('pyhik.hikvision._BROKEN_ALERT_STREAM_DEVICES',
                   _BROKEN_ALERT_STREAM_DEVICES + [additional]):
            camera = make_camera(
                model=additional[0],
                firmwareVersion=additional[1],
                firmwareReleasedDate=additional[2],
            )
            camera.start_stream()
            self.assertIsNone(camera.thrd.ident)
            camera.disconnect()

    def test_other_models_or_firmware_still_start_stream(self):
        variants = (
            {'model': 'DS-2CD2086G2-IU'},
            {'model': 'DS-2CD2087G3-LI2UY'},
            {'model': 'DS-KV8113-WME1(B)', 'firmwareVersion': 'V2.2.54'},
            {'model': 'DS-KV8113-WME1(B)', 'firmwareReleasedDate': 'build 230919'},
            {'model': 'Other'},
        )
        for variant in variants:
            with self.subTest(info=variant):
                camera = make_camera(**variant)
                camera.thrd = MagicMock()
                camera.start_stream()
                camera.thrd.start.assert_called_once_with()

    def test_missing_device_info_preserves_default_behavior(self):
        cases = (
            {'model': None},
            {'firmwareVersion': None},
            {'firmwareReleasedDate': None},
            {'model': ''},
        )
        for variant in cases:
            with self.subTest(info=variant):
                camera = make_camera(**variant)
                camera.thrd = MagicMock()
                camera.start_stream()
                camera.thrd.start.assert_called_once_with()

    def test_metadata_whitespace_is_ignored(self):
        camera = make_camera(
            model=' DS-KV8113-WME1(B) ',
            firmwareVersion=' V2.2.53\n',
            firmwareReleasedDate='  build 230918  ',
        )
        camera.start_stream()
        self.assertIsNone(camera.thrd.ident)
        camera.disconnect()

    def test_disconnect_on_never_started_thread_is_idempotent(self):
        camera = make_camera()
        camera.start_stream()
        with patch.object(camera.hik_request_stream, 'close') as close:
            camera.disconnect()
            camera.disconnect()
        self.assertEqual(close.call_count, 2)
        self.assertFalse(camera.kill_thrd.is_set())

    def test_disconnect_on_started_thread_still_joins(self):
        camera = make_camera(model='Other')
        camera.thrd = MagicMock()
        camera.thrd.ident = 12345
        camera.disconnect()
        camera.thrd.join.assert_called_once_with()

    def test_snapshot_and_rtsp_dont_depend_on_event_thread(self):
        camera = make_camera()
        camera.start_stream()
        camera.hik_request.get = MagicMock(
            return_value=MagicMock(status_code=200, content=b'JPEG')
        )
        self.assertEqual(camera.get_snapshot(1), b'JPEG')
        self.assertIn('/Streaming/Channels/1', camera.get_stream_url(1))
        camera.disconnect()


if __name__ == '__main__':
    unittest.main()
