#!/usr/bin/env python3

import logging
import requests
import unittest

from unittest.mock import MagicMock, patch, PropertyMock
from requests.auth import HTTPDigestAuth
from pyhik.hikvision import HikCamera, inject_events_into_camera
from pyhik.constants import CONNECT_TIMEOUT, VALID_NOTIFICATION_METHODS

XML = """<MotionDetection xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
    <enabled>{}</enabled>
    <enableHighlight>true</enableHighlight>
    <samplingInterval>2</samplingInterval>
    <startTriggerTime>500</startTriggerTime>
    <endTriggerTime>500</endTriggerTime>
    <regionType>grid</regionType>
    <Grid>
        <rowGranularity>18</rowGranularity>
        <columnGranularity>22</columnGranularity>
    </Grid>
    <MotionDetectionLayout version="2.0">
        <sensitivityLevel>20</sensitivityLevel>
        <layout>
            <gridMap>000000000000000000000000000000000c007e0c007ffffc</gridMap>
        </layout>
    </MotionDetectionLayout>
</MotionDetection>"""


@patch("pyhik.hikvision.requests.Session")
class HikvisionTestCase(unittest.TestCase):
    @staticmethod
    def set_motion_detection_state(get, value):
        get.reset_mock()
        mock = get.return_value
        mock.reset_mock()
        type(mock).ok = PropertyMock(return_value=True)
        type(mock).status_code = PropertyMock(return_value=requests.codes.ok)
        type(mock).text = PropertyMock(
            return_value=XML.format("true" if value else "false")
        )
        return get

    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    def test_motion_detection(self, *args):

        session = args[-1].return_value
        get = session.get
        url = "http://localhost:80/ISAPI/System/Video/inputs/channels/1/motionDetection"

        # Motion detection disabled
        self.set_motion_detection_state(get, False)
        device = HikCamera(host="localhost")
        get.assert_called_once_with(url, timeout=CONNECT_TIMEOUT)
        self.assertIsNotNone(device)
        self.assertFalse(device.current_motion_detection_state)

        # Motion detection enabled
        self.set_motion_detection_state(get, True)
        device = HikCamera(host="localhost")
        self.assertIsNotNone(device)
        self.assertTrue(device.current_motion_detection_state)

        # Enable calls put with the expected data
        self.set_motion_detection_state(get, True)
        session.put.return_value = MagicMock(status_code=requests.codes.ok, ok=True)
        device.enable_motion_detection()
        put_url = "http://localhost:80/ISAPI/System/Video/inputs/channels/1/motionDetection"
        session.put.assert_called_once_with(put_url, data=XML.format("true").encode(), timeout=CONNECT_TIMEOUT)

        # Disable
        def change_get_response(url, data,timeout):
            self.set_motion_detection_state(get, False)
            return MagicMock(ok=True, status_code=requests.codes.ok)

        self.set_motion_detection_state(get, True)
        session.put = MagicMock(side_effect=change_get_response)
        device = HikCamera(host="localhost")
        self.assertTrue(device.current_motion_detection_state)
        device.disable_motion_detection()
        self.assertFalse(device.current_motion_detection_state)


# XML for testing get_event_triggers with various notification methods
EVENT_TRIGGERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<EventTriggerList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
    <EventTrigger version="2.0">
        <id>1</id>
        <eventType>VMD</eventType>
        <videoInputChannelID>1</videoInputChannelID>
        <EventTriggerNotificationList>
            <EventTriggerNotification>
                <id>1</id>
                <notificationMethod>record</notificationMethod>
            </EventTriggerNotification>
        </EventTriggerNotificationList>
    </EventTrigger>
    <EventTrigger version="2.0">
        <id>2</id>
        <eventType>linedetection</eventType>
        <videoInputChannelID>2</videoInputChannelID>
        <EventTriggerNotificationList>
            <EventTriggerNotification>
                <id>1</id>
                <notificationMethod>email</notificationMethod>
            </EventTriggerNotification>
        </EventTriggerNotificationList>
    </EventTrigger>
    <EventTrigger version="2.0">
        <id>3</id>
        <eventType>fielddetection</eventType>
        <videoInputChannelID>3</videoInputChannelID>
        <EventTriggerNotificationList>
            <EventTriggerNotification>
                <id>1</id>
                <notificationMethod>beep</notificationMethod>
            </EventTriggerNotification>
        </EventTriggerNotificationList>
    </EventTrigger>
    <EventTrigger version="2.0">
        <id>4</id>
        <eventType>VMD</eventType>
        <videoInputChannelID>4</videoInputChannelID>
        <EventTriggerNotificationList>
            <EventTriggerNotification>
                <id>1</id>
                <notificationMethod>center</notificationMethod>
            </EventTriggerNotification>
        </EventTriggerNotificationList>
    </EventTrigger>
    <EventTrigger version="2.0">
        <id>5</id>
        <eventType>VMD</eventType>
        <videoInputChannelID>5</videoInputChannelID>
        <EventTriggerNotificationList>
            <EventTriggerNotification>
                <id>1</id>
                <notificationMethod>HTTP</notificationMethod>
            </EventTriggerNotification>
        </EventTriggerNotificationList>
    </EventTrigger>
</EventTriggerList>"""


class GetEventTriggersTestCase(unittest.TestCase):
    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.get_device_info")
    def test_default_notification_methods(self, mock_info, mock_session):
        """Test that get_event_triggers defaults to center and HTTP only."""
        mock_info.return_value = {"deviceName": "Test", "deviceID": "12345678901"}
        session = mock_session.return_value
        response = MagicMock()
        response.status_code = requests.codes.ok
        response.text = EVENT_TRIGGERS_XML
        session.get.return_value = response

        camera = HikCamera(host="localhost")
        # Call get_event_triggers with default (no args)
        events = camera.get_event_triggers()

        # Should only find VMD on channels 4 and 5 (center and HTTP)
        self.assertIn("VMD", events)
        self.assertEqual(sorted(events["VMD"]), [4, 5])

        # Should NOT find events with record, email, beep notification methods
        self.assertNotIn("linedetection", events)
        self.assertNotIn("fielddetection", events)

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.get_device_info")
    def test_custom_notification_methods(self, mock_info, mock_session):
        """Test that get_event_triggers accepts custom notification methods."""
        mock_info.return_value = {"deviceName": "Test", "deviceID": "12345678901"}
        session = mock_session.return_value
        response = MagicMock()
        response.status_code = requests.codes.ok
        response.text = EVENT_TRIGGERS_XML
        session.get.return_value = response

        camera = HikCamera(host="localhost")
        # Call get_event_triggers with expanded notification methods
        events = camera.get_event_triggers(
            notification_methods={'center', 'HTTP', 'record', 'email', 'beep'}
        )

        # Should find VMD on channels 1, 4, 5 (record, center, HTTP)
        self.assertIn("VMD", events)
        self.assertEqual(sorted(events["VMD"]), [1, 4, 5])

        # Should find linedetection on channel 2 (email)
        self.assertIn("linedetection", events)
        self.assertEqual(events["linedetection"], [2])

        # Should find fielddetection on channel 3 (beep)
        self.assertIn("fielddetection", events)
        self.assertEqual(events["fielddetection"], [3])

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.get_device_info")
    def test_valid_notification_methods_constant(self, mock_info, mock_session):
        """Test using VALID_NOTIFICATION_METHODS constant."""
        mock_info.return_value = {"deviceName": "Test", "deviceID": "12345678901"}
        session = mock_session.return_value
        response = MagicMock()
        response.status_code = requests.codes.ok
        response.text = EVENT_TRIGGERS_XML
        session.get.return_value = response

        camera = HikCamera(host="localhost")
        # Use the exported constant
        events = camera.get_event_triggers(
            notification_methods=VALID_NOTIFICATION_METHODS
        )

        # Should find all events
        self.assertIn("VMD", events)
        self.assertEqual(sorted(events["VMD"]), [1, 4, 5])
        self.assertIn("linedetection", events)
        self.assertIn("fielddetection", events)

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.get_device_info")
    def test_case_insensitive_notification_methods(self, mock_info, mock_session):
        """Test that notification method matching is case insensitive."""
        mock_info.return_value = {"deviceName": "Test", "deviceID": "12345678901"}
        session = mock_session.return_value
        response = MagicMock()
        response.status_code = requests.codes.ok
        response.text = EVENT_TRIGGERS_XML
        session.get.return_value = response

        camera = HikCamera(host="localhost")
        # Use uppercase - should still match lowercase in XML
        events = camera.get_event_triggers(
            notification_methods={'CENTER', 'http', 'RECORD'}
        )

        # Should find VMD on channels 1, 4, 5
        self.assertIn("VMD", events)
        self.assertEqual(sorted(events["VMD"]), [1, 4, 5])


class URLParsingTestCase(unittest.TestCase):
    """Test that URL parsing handles various host formats correctly."""

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.initialize")
    def test_plain_host(self, mock_init, mock_session):
        """Test host as plain IP address."""
        camera = HikCamera(host="192.168.1.100", port=80)
        self.assertEqual(camera.host, "192.168.1.100")
        self.assertEqual(camera.root_url, "http://192.168.1.100:80")

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.initialize")
    def test_host_with_scheme(self, mock_init, mock_session):
        """Test host as URL with http scheme."""
        camera = HikCamera(host="http://192.168.1.100", port=80)
        self.assertEqual(camera.host, "192.168.1.100")
        self.assertEqual(camera.root_url, "http://192.168.1.100:80")

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.initialize")
    def test_host_with_scheme_and_port(self, mock_init, mock_session):
        """Test host as URL with scheme and port - port in URL takes precedence."""
        camera = HikCamera(host="http://192.168.1.100:8080", port=80)
        self.assertEqual(camera.host, "192.168.1.100")
        self.assertEqual(camera.root_url, "http://192.168.1.100:8080")

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.initialize")
    def test_host_with_https(self, mock_init, mock_session):
        """Test host with https scheme."""
        camera = HikCamera(host="https://192.168.1.100", port=443)
        self.assertEqual(camera.host, "192.168.1.100")
        self.assertEqual(camera.root_url, "https://192.168.1.100:443")

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.initialize")
    def test_hostname(self, mock_init, mock_session):
        """Test with hostname instead of IP."""
        camera = HikCamera(host="camera.local", port=80)
        self.assertEqual(camera.host, "camera.local")
        self.assertEqual(camera.root_url, "http://camera.local:80")


class InjectEventsTestCase(unittest.TestCase):
    def test_inject_events_adds_new_events(self):
        """Test that inject_events adds new events to camera event_states."""
        camera = MagicMock()
        camera.event_states = {}

        events = {
            "Motion": [1, 2],
            "Line Crossing": [3]
        }

        inject_events_into_camera(camera, events)

        camera.inject_events.assert_called_once_with(events)

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    def test_inject_events_method(self, mock_triggers, mock_info, mock_session):
        """Test that HikCamera.inject_events correctly adds events."""
        mock_info.return_value = {"deviceName": "Test", "deviceID": "12345678901"}
        mock_triggers.return_value = {}
        session = mock_session.return_value
        session.get.return_value = MagicMock(status_code=requests.codes.not_found)

        camera = HikCamera(host="localhost")
        camera.event_states = {}

        # Inject events
        events = {
            "Motion": [1, 2],
            "Line Crossing": [3]
        }
        camera.inject_events(events)

        # Verify events were added
        self.assertIn("Motion", camera.event_states)
        self.assertEqual(len(camera.event_states["Motion"]), 2)
        self.assertEqual(camera.event_states["Motion"][0][1], 1)  # channel 1
        self.assertEqual(camera.event_states["Motion"][1][1], 2)  # channel 2
        self.assertFalse(camera.event_states["Motion"][0][0])  # not active

        self.assertIn("Line Crossing", camera.event_states)
        self.assertEqual(len(camera.event_states["Line Crossing"]), 1)
        self.assertEqual(camera.event_states["Line Crossing"][0][1], 3)  # channel 3

    @patch("pyhik.hikvision.requests.Session")
    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    def test_inject_events_does_not_duplicate(self, mock_triggers, mock_info, mock_session):
        """Test that inject_events doesn't add duplicate channel events."""
        mock_info.return_value = {"deviceName": "Test", "deviceID": "12345678901"}
        mock_triggers.return_value = {}
        session = mock_session.return_value
        session.get.return_value = MagicMock(status_code=requests.codes.not_found)

        camera = HikCamera(host="localhost")
        camera.event_states = {
            "Motion": [[False, 1, 0, None]]  # Already has channel 1
        }

        # Try to inject event for same channel
        events = {"Motion": [1, 2]}
        camera.inject_events(events)

        # Should only have 2 entries (original + channel 2, not duplicate of 1)
        self.assertEqual(len(camera.event_states["Motion"]), 2)
        channels = [sensor[1] for sensor in camera.event_states["Motion"]]
        self.assertEqual(sorted(channels), [1, 2])


class ThreadSafetyTestCase(unittest.TestCase):
    """Tests for thread-safe session separation between API and stream."""

    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    @patch("pyhik.hikvision.requests.Session")
    def test_separate_sessions_created(self, mock_session_cls, mock_triggers, mock_info):
        """Test that two separate sessions are created for API and stream."""
        mock_info.return_value = {"deviceName": "Test", "deviceID": "12345678901"}
        mock_triggers.return_value = {}

        api_session = MagicMock(name="api_session")
        stream_session = MagicMock(name="stream_session")
        api_session.get.return_value = MagicMock(status_code=requests.codes.not_found)
        mock_session_cls.side_effect = [api_session, stream_session]

        camera = HikCamera(host="localhost")

        # requests.Session() should be called twice
        self.assertEqual(mock_session_cls.call_count, 2)

        # The two sessions should be different objects
        self.assertIsNot(camera.hik_request, camera.hik_request_stream)
        self.assertIs(camera.hik_request, api_session)
        self.assertIs(camera.hik_request_stream, stream_session)

        # Both should have auth and headers configured
        api_session.headers.update.assert_called_once()
        stream_session.headers.update.assert_called_once()

    @patch("pyhik.hikvision.requests.Session")
    def test_digest_auth_updates_both_sessions(self, mock_session_cls):
        """Test that auth negotiation falling back to digest updates both sessions."""
        api_session = MagicMock(name="api_session")
        stream_session = MagicMock(name="stream_session")
        mock_session_cls.side_effect = [api_session, stream_session]

        # First call returns 401 (basic auth failed), second call succeeds
        device_info_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            '<deviceName>TestCam</deviceName>'
            '<deviceID>12345678901</deviceID>'
            '</DeviceInfo>'
        )
        unauthorized_resp = MagicMock(status_code=requests.codes.unauthorized)
        ok_resp = MagicMock(status_code=requests.codes.ok, text=device_info_xml)
        # get_device_info: 1st call → 401, 2nd call (digest) → 200
        # get_event_triggers: → not_found (skip)
        # get_motion_detection: → not_found (skip)
        not_found_resp = MagicMock(status_code=requests.codes.not_found)
        api_session.get.side_effect = [unauthorized_resp, ok_resp, not_found_resp, not_found_resp, not_found_resp]

        camera = HikCamera(host="localhost", usr="admin", pwd="pass")

        # Both sessions should have been switched to digest auth
        self.assertIsInstance(api_session.auth, HTTPDigestAuth)
        self.assertIsInstance(stream_session.auth, HTTPDigestAuth)

    @patch("pyhik.hikvision.HikCamera.get_motion_detection")
    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    @patch("pyhik.hikvision.requests.Session")
    def test_snapshot_uses_api_session(
        self, mock_session_cls, mock_triggers, mock_info, mock_motion
    ):
        """Test that get_snapshot uses the API session, not the stream session."""
        mock_info.return_value = {"deviceName": "Test", "deviceID": "12345678901"}
        mock_triggers.return_value = {}

        api_session = MagicMock(name="api_session")
        stream_session = MagicMock(name="stream_session")
        mock_session_cls.side_effect = [api_session, stream_session]

        camera = HikCamera(host="localhost")

        # Reset call counts from initialization, then set up snapshot response
        api_session.get.reset_mock()
        api_session.get.return_value = MagicMock(
            status_code=requests.codes.ok, content=b"image_data"
        )

        result = camera.get_snapshot()

        # API session should have been called for the snapshot
        snapshot_url = "localhost:80/ISAPI/Streaming/channels/1/picture"
        api_session.get.assert_called_with(snapshot_url, timeout=10)

        # Stream session should NOT have been called for the snapshot
        stream_session.get.assert_not_called()
        self.assertEqual(result, b"image_data")

    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    @patch("pyhik.hikvision.requests.Session")
    def test_closing_stream_session_does_not_affect_api_session(
        self, mock_session_cls, mock_triggers, mock_info
    ):
        """Test that closing the stream session doesn't close the API session."""
        mock_info.return_value = {"deviceName": "Test", "deviceID": "12345678901"}
        mock_triggers.return_value = {}

        api_session = MagicMock(name="api_session")
        stream_session = MagicMock(name="stream_session")
        api_session.get.return_value = MagicMock(status_code=requests.codes.not_found)
        mock_session_cls.side_effect = [api_session, stream_session]

        camera = HikCamera(host="localhost")

        # Simulate what alert_stream does on reconnect
        camera.hik_request_stream.close()

        # Stream session should be closed
        stream_session.close.assert_called_once()

        # API session should NOT be closed
        api_session.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
