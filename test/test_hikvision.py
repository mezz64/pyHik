#!/usr/bin/env python3

import logging
import requests
import unittest

from unittest.mock import MagicMock, patch, PropertyMock
from pyhik.hikvision import HikCamera
from pyhik.constants import (CONNECT_TIMEOUT, HikvisionChannel)

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

VIDEO_INPUT_CHANNELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<VideoInputChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
    <VideoInputChannel version="2.0">
        <id>1</id>
        <name>Front Door</name>
        <videoInputEnabled>true</videoInputEnabled>
    </VideoInputChannel>
    <VideoInputChannel version="2.0">
        <id>2</id>
        <name>Back Yard</name>
        <videoInputEnabled>true</videoInputEnabled>
    </VideoInputChannel>
</VideoInputChannelList>"""

INPUT_PROXY_CHANNELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
    <InputProxyChannel>
        <id>1</id>
        <name>Camera 1</name>
        <enabled>true</enabled>
        <online>true</online>
    </InputProxyChannel>
    <InputProxyChannel>
        <id>2</id>
        <name>Camera 2</name>
        <enabled>true</enabled>
        <online>false</online>
    </InputProxyChannel>
</InputProxyChannelList>"""

STREAMING_CHANNELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<StreamingChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
    <StreamingChannel version="2.0">
        <id>101</id>
        <channelName>Main Stream 1</channelName>
        <enabled>true</enabled>
    </StreamingChannel>
    <StreamingChannel version="2.0">
        <id>102</id>
        <channelName>Sub Stream 1</channelName>
        <enabled>true</enabled>
    </StreamingChannel>
    <StreamingChannel version="2.0">
        <id>201</id>
        <channelName>Main Stream 2</channelName>
        <enabled>false</enabled>
    </StreamingChannel>
</StreamingChannelList>"""


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
        url = "localhost:80/ISAPI/System/Video/inputs/channels/1/motionDetection"

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
        session.put.assert_called_once_with(url, data=XML.format("true").encode(), timeout=CONNECT_TIMEOUT)

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

    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    @patch("pyhik.hikvision.HikCamera.get_motion_detection")
    def test_get_video_channels_video_input(self, *args):
        """Test get_video_channels with VideoInputChannel XML response."""
        session = args[-1].return_value
        get = session.get

        # Create device
        device = HikCamera(host="localhost", usr="admin", pwd="password")

        # Mock get_video_channels response
        mock_response = MagicMock()
        mock_response.status_code = requests.codes.ok
        mock_response.text = VIDEO_INPUT_CHANNELS_XML
        get.return_value = mock_response

        channels = device.get_video_channels()

        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0].id, 1)
        self.assertEqual(channels[0].name, "Front Door")
        self.assertTrue(channels[0].enabled)
        self.assertEqual(channels[1].id, 2)
        self.assertEqual(channels[1].name, "Back Yard")

    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    @patch("pyhik.hikvision.HikCamera.get_motion_detection")
    def test_get_video_channels_input_proxy(self, *args):
        """Test get_video_channels with InputProxyChannel XML (NVR)."""
        session = args[-1].return_value
        get = session.get

        device = HikCamera(host="localhost", usr="admin", pwd="password")

        # First endpoint returns 404, second returns InputProxyChannel XML
        mock_404 = MagicMock()
        mock_404.status_code = requests.codes.not_found

        mock_response = MagicMock()
        mock_response.status_code = requests.codes.ok
        mock_response.text = INPUT_PROXY_CHANNELS_XML

        get.side_effect = [mock_404, mock_response]

        channels = device.get_video_channels()

        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0].id, 1)
        self.assertEqual(channels[0].name, "Camera 1")
        self.assertTrue(channels[0].enabled)  # enabled=true and online=true
        self.assertEqual(channels[1].id, 2)
        self.assertEqual(channels[1].name, "Camera 2")
        self.assertFalse(channels[1].enabled)  # enabled=true but online=false

    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    @patch("pyhik.hikvision.HikCamera.get_motion_detection")
    def test_get_video_channels_streaming(self, *args):
        """Test get_video_channels fallback to StreamingChannel XML."""
        session = args[-1].return_value
        get = session.get

        device = HikCamera(host="localhost", usr="admin", pwd="password")

        # First two endpoints return 404, streaming endpoint works
        mock_404 = MagicMock()
        mock_404.status_code = requests.codes.not_found

        mock_response = MagicMock()
        mock_response.status_code = requests.codes.ok
        mock_response.text = STREAMING_CHANNELS_XML

        get.side_effect = [mock_404, mock_404, mock_response]

        channels = device.get_video_channels()

        # Should deduplicate channels (101, 102 -> channel 1; 201 -> channel 2)
        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0].id, 1)
        self.assertEqual(channels[0].name, "Main Stream 1")
        self.assertTrue(channels[0].enabled)
        self.assertEqual(channels[1].id, 2)
        self.assertEqual(channels[1].name, "Main Stream 2")
        self.assertFalse(channels[1].enabled)

    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    @patch("pyhik.hikvision.HikCamera.get_motion_detection")
    def test_build_rtsp_url(self, *args):
        """Test RTSP URL construction."""
        session = args[-1].return_value

        device = HikCamera(host="http://192.168.1.100", usr="admin", pwd="secret")

        # Main stream (type 1) for channel 1
        url = device.build_rtsp_url(channel=1, stream_type=1)
        self.assertEqual(url, "rtsp://admin:secret@192.168.1.100:554/Streaming/Channels/101")

        # Sub stream (type 2) for channel 2
        url = device.build_rtsp_url(channel=2, stream_type=2)
        self.assertEqual(url, "rtsp://admin:secret@192.168.1.100:554/Streaming/Channels/202")

        # Custom RTSP port
        url = device.build_rtsp_url(channel=1, stream_type=1, rtsp_port=8554)
        self.assertEqual(url, "rtsp://admin:secret@192.168.1.100:8554/Streaming/Channels/101")

    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    @patch("pyhik.hikvision.HikCamera.get_motion_detection")
    def test_build_snapshot_url(self, *args):
        """Test snapshot URL construction."""
        session = args[-1].return_value

        device = HikCamera(host="http://192.168.1.100", port=80, usr="admin", pwd="secret")

        # Main stream snapshot for channel 1
        url = device.build_snapshot_url(channel=1, stream_type=1)
        self.assertEqual(url, "http://192.168.1.100:80/ISAPI/Streaming/channels/101/picture")

        # Sub stream snapshot for channel 2
        url = device.build_snapshot_url(channel=2, stream_type=2)
        self.assertEqual(url, "http://192.168.1.100:80/ISAPI/Streaming/channels/202/picture")


if __name__ == "__main__":
    unittest.main()
