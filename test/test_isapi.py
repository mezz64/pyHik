#!/usr/bin/env python3
"""Tests for pyhik.isapi module."""

import unittest
from unittest.mock import MagicMock, patch, PropertyMock

import requests

from pyhik.isapi import (
    ISAPIClient,
    ISAPIError,
    ISAPIConnectionError,
    ISAPIAuthError,
    ISAPINotFoundError,
    HTTPMethod,
    StorageDevice,
    AlarmServerInfo,
    StreamInfo,
    CameraInfo,
    OutputPort,
    InputPort,
    EventState,
    DeviceCapabilities,
)


# Sample XML responses
DEVICE_INFO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <deviceName>Test Camera</deviceName>
    <deviceID>12345678901234567890</deviceID>
    <model>DS-2CD2142FWD-I</model>
    <serialNumber>DS-2CD2142FWD-I20170101AAWRC12345678</serialNumber>
    <macAddress>aa:bb:cc:dd:ee:ff</macAddress>
    <firmwareVersion>V5.4.5</firmwareVersion>
    <deviceType>IPCamera</deviceType>
</DeviceInfo>"""

STREAMING_CHANNELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<StreamingChannelList version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <StreamingChannel version="2.0">
        <id>101</id>
        <channelName>Camera 01</channelName>
        <enabled>true</enabled>
    </StreamingChannel>
    <StreamingChannel version="2.0">
        <id>102</id>
        <channelName>Camera 01</channelName>
        <enabled>true</enabled>
    </StreamingChannel>
    <StreamingChannel version="2.0">
        <id>201</id>
        <channelName>Camera 02</channelName>
        <enabled>true</enabled>
    </StreamingChannel>
</StreamingChannelList>"""

IO_OUTPUTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<IOOutputPortList version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <IOOutputPort version="2.0">
        <id>1</id>
        <outputName>Alarm Output 1</outputName>
    </IOOutputPort>
</IOOutputPortList>"""

STORAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<storage version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <hddList>
        <hdd>
            <id>1</id>
            <hddName>HDD1</hddName>
            <status>ok</status>
            <capacity>500000</capacity>
            <freeSpace>250000</freeSpace>
        </hdd>
    </hddList>
</storage>"""


class TestISAPIClientBasics(unittest.TestCase):
    """Test ISAPIClient initialization and basic functionality."""

    def test_init_defaults(self):
        """Test client initialization with defaults."""
        client = ISAPIClient(host="192.168.1.100")
        self.assertEqual(client.host, "192.168.1.100")
        self.assertEqual(client.port, 80)
        self.assertEqual(client.base_url, "http://192.168.1.100:80")
        self.assertFalse(client.ssl)
        self.assertEqual(client.rtsp_port, 554)

    def test_init_ssl(self):
        """Test client initialization with SSL."""
        client = ISAPIClient(host="192.168.1.100", port=443, ssl=True)
        self.assertEqual(client.base_url, "https://192.168.1.100:443")

    def test_get_rtsp_url(self):
        """Test RTSP URL generation."""
        client = ISAPIClient(
            host="192.168.1.100",
            username="admin",
            password="pass123",
        )
        url = client.get_rtsp_url(channel=1, stream_type=1)
        self.assertEqual(
            url,
            "rtsp://admin:pass123@192.168.1.100:554/Streaming/Channels/101"
        )

    def test_get_rtsp_url_substream(self):
        """Test RTSP URL generation for substream."""
        client = ISAPIClient(
            host="192.168.1.100",
            username="admin",
            password="pass123",
        )
        url = client.get_rtsp_url(channel=2, stream_type=2)
        self.assertEqual(
            url,
            "rtsp://admin:pass123@192.168.1.100:554/Streaming/Channels/202"
        )

    def test_get_rtsp_url_without_credentials(self):
        """Test RTSP URL generation without credentials."""
        client = ISAPIClient(
            host="192.168.1.100",
            username="admin",
            password="pass123",
        )
        url = client.get_rtsp_url(channel=1, include_credentials=False)
        self.assertEqual(
            url,
            "rtsp://192.168.1.100:554/Streaming/Channels/101"
        )

    def test_host_with_scheme(self):
        """Test that host with http:// scheme is parsed correctly."""
        client = ISAPIClient(host="http://192.168.1.100", port=80)
        self.assertEqual(client.host, "192.168.1.100")
        self.assertEqual(client.port, 80)
        self.assertEqual(client.base_url, "http://192.168.1.100:80")

    def test_host_with_scheme_and_port(self):
        """Test that host with scheme and port extracts port from URL."""
        client = ISAPIClient(host="http://192.168.1.100:8080", port=80)
        self.assertEqual(client.host, "192.168.1.100")
        self.assertEqual(client.port, 8080)
        self.assertEqual(client.base_url, "http://192.168.1.100:8080")

    def test_host_with_https_scheme(self):
        """Test that host with https:// scheme is parsed correctly."""
        client = ISAPIClient(host="https://192.168.1.100", port=443, ssl=True)
        self.assertEqual(client.host, "192.168.1.100")
        self.assertEqual(client.base_url, "https://192.168.1.100:443")

    def test_rtsp_url_special_chars_in_password(self):
        """Test that RTSP URL encodes special characters in credentials."""
        client = ISAPIClient(
            host="192.168.1.100",
            username="admin",
            password="p@ss:word/123",
        )
        url = client.get_rtsp_url(channel=1, stream_type=1)
        self.assertIn("admin", url)
        self.assertIn("p%40ss%3Aword%2F123", url)
        self.assertNotIn("p@ss:word/123", url)

    def test_context_manager(self):
        """Test context manager usage."""
        with ISAPIClient(host="192.168.1.100") as client:
            self.assertIsNotNone(client)


@patch("pyhik.isapi.requests.Session")
class TestISAPIClientRequests(unittest.TestCase):
    """Test ISAPIClient HTTP request handling."""

    def test_auth_detection_digest(self, mock_session_class):
        """Test digest auth detection."""
        session = mock_session_class.return_value
        response = MagicMock()
        response.status_code = 200
        response.text = DEVICE_INFO_XML
        response.headers = {"content-type": "application/xml"}
        session.get.return_value = response

        client = ISAPIClient(
            host="192.168.1.100",
            username="admin",
            password="password",
        )
        client.get_device_info()

        # Should have tried digest auth
        self.assertIsNotNone(client._auth)

    def test_connection_error(self, mock_session_class):
        """Test connection error handling."""
        session = mock_session_class.return_value
        session.get.side_effect = requests.exceptions.ConnectionError("Failed")

        client = ISAPIClient(host="192.168.1.100")
        with self.assertRaises(ISAPIConnectionError):
            client.get_device_info()

    def test_timeout_error(self, mock_session_class):
        """Test timeout error handling."""
        session = mock_session_class.return_value
        session.get.side_effect = requests.exceptions.Timeout("Timeout")

        client = ISAPIClient(host="192.168.1.100")
        with self.assertRaises(ISAPIConnectionError):
            client.get_device_info()

    def test_auth_error_401(self, mock_session_class):
        """Test 401 authentication error."""
        session = mock_session_class.return_value
        response = MagicMock()
        response.status_code = 401
        session.get.return_value = response

        client = ISAPIClient(host="192.168.1.100")
        client._auth = MagicMock()  # Skip auth detection
        with self.assertRaises(ISAPIAuthError):
            client.get_device_info()

    def test_not_found_error(self, mock_session_class):
        """Test 404 not found error."""
        session = mock_session_class.return_value
        response = MagicMock()
        response.status_code = 404
        session.get.return_value = response

        client = ISAPIClient(host="192.168.1.100")
        client._auth = MagicMock()
        with self.assertRaises(ISAPINotFoundError):
            client.request(HTTPMethod.GET, "/ISAPI/nonexistent")


@patch("pyhik.isapi.requests.Session")
@patch("pyhik.isapi.xmltodict")
class TestISAPIClientMethods(unittest.TestCase):
    """Test ISAPIClient API methods."""

    def _setup_response(self, session, xml_content, content_type="application/xml"):
        """Helper to set up mock response."""
        response = MagicMock()
        response.status_code = 200
        response.text = xml_content
        response.headers = {"content-type": content_type}
        session.get.return_value = response
        session.put.return_value = response
        return response

    def test_get_device_info(self, mock_xmltodict, mock_session_class):
        """Test getting device info."""
        session = mock_session_class.return_value
        self._setup_response(session, DEVICE_INFO_XML)
        mock_xmltodict.parse.return_value = {
            "DeviceInfo": {
                "deviceName": "Test Camera",
                "serialNumber": "ABC123",
                "model": "DS-2CD2142FWD-I",
                "deviceType": "IPCamera",
                "firmwareVersion": "V5.4.5",
            }
        }

        client = ISAPIClient(host="192.168.1.100")
        info = client.get_device_info()

        self.assertEqual(info.get("deviceName"), "Test Camera")
        self.assertEqual(client.get_device_name(), "Test Camera")
        self.assertEqual(client.get_device_serial(), "ABC123")
        self.assertEqual(client.get_device_model(), "DS-2CD2142FWD-I")
        self.assertEqual(client.get_device_type(), "IPCamera")
        self.assertEqual(client.get_firmware_version(), "V5.4.5")

    def test_get_streaming_channels(self, mock_xmltodict, mock_session_class):
        """Test getting streaming channels."""
        session = mock_session_class.return_value
        self._setup_response(session, STREAMING_CHANNELS_XML)
        mock_xmltodict.parse.return_value = {
            "StreamingChannelList": {
                "StreamingChannel": [
                    {"id": "101", "channelName": "Camera 01", "enabled": "true"},
                    {"id": "102", "channelName": "Camera 01", "enabled": "true"},
                    {"id": "201", "channelName": "Camera 02", "enabled": "true"},
                ]
            }
        }

        client = ISAPIClient(host="192.168.1.100")
        client._auth = MagicMock()
        streams = client.get_streaming_channels()

        self.assertEqual(len(streams), 3)
        self.assertEqual(streams[0].channel_id, 1)
        self.assertEqual(streams[0].type_id, 1)
        self.assertEqual(streams[2].channel_id, 2)

    def test_get_cameras(self, mock_xmltodict, mock_session_class):
        """Test getting camera list."""
        session = mock_session_class.return_value
        self._setup_response(session, STREAMING_CHANNELS_XML)
        mock_xmltodict.parse.return_value = {
            "StreamingChannelList": {
                "StreamingChannel": [
                    {"id": "101", "channelName": "Camera 01", "enabled": "true"},
                    {"id": "102", "channelName": "Camera 01", "enabled": "true"},
                    {"id": "201", "channelName": "Camera 02", "enabled": "true"},
                ]
            }
        }

        client = ISAPIClient(host="192.168.1.100")
        client._auth = MagicMock()
        cameras = client.get_cameras()

        self.assertEqual(len(cameras), 2)
        self.assertEqual(cameras[0].id, 1)
        self.assertEqual(cameras[0].name, "Camera 01")
        self.assertEqual(len(cameras[0].streams), 2)
        self.assertEqual(cameras[1].id, 2)

    def test_get_output_ports(self, mock_xmltodict, mock_session_class):
        """Test getting output ports."""
        session = mock_session_class.return_value
        self._setup_response(session, IO_OUTPUTS_XML)
        mock_xmltodict.parse.return_value = {
            "IOOutputPortList": {
                "IOOutputPort": {
                    "id": "1",
                    "outputName": "Alarm Output 1",
                }
            }
        }

        client = ISAPIClient(host="192.168.1.100")
        client._auth = MagicMock()
        ports = client.get_output_ports()

        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0].id, "1")
        self.assertEqual(ports[0].name, "Alarm Output 1")

    def test_get_storage_devices(self, mock_xmltodict, mock_session_class):
        """Test getting storage devices."""
        session = mock_session_class.return_value
        self._setup_response(session, STORAGE_XML)
        mock_xmltodict.parse.return_value = {
            "storage": {
                "hddList": {
                    "hdd": {
                        "id": "1",
                        "hddName": "HDD1",
                        "status": "ok",
                        "capacity": "500000",
                        "freeSpace": "250000",
                    }
                }
            }
        }

        client = ISAPIClient(host="192.168.1.100")
        client._auth = MagicMock()
        devices = client.get_storage_devices()

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, "HDD1")
        self.assertEqual(devices[0].status, "ok")
        self.assertEqual(devices[0].type, "HDD")
        # Capacity is in MB, should be converted to bytes
        self.assertEqual(devices[0].capacity, 500000 * 1024 * 1024)

    def test_get_snapshot(self, mock_xmltodict, mock_session_class):
        """Test getting snapshot."""
        session = mock_session_class.return_value
        response = MagicMock()
        response.status_code = 200
        response.content = b"\xff\xd8\xff\xe0"  # JPEG magic bytes
        response.headers = {"content-type": "image/jpeg"}
        session.get.return_value = response

        client = ISAPIClient(host="192.168.1.100")
        client._auth = MagicMock()
        snapshot = client.get_snapshot(channel=1)

        self.assertEqual(snapshot, b"\xff\xd8\xff\xe0")


class TestDataClasses(unittest.TestCase):
    """Test data classes."""

    def test_storage_device(self):
        """Test StorageDevice dataclass."""
        device = StorageDevice(
            id="1",
            name="HDD1",
            status="ok",
            type="HDD",
            capacity=1000000,
            free_space=500000,
        )
        self.assertEqual(device.id, "1")
        self.assertEqual(device.name, "HDD1")

    def test_camera_info(self):
        """Test CameraInfo dataclass."""
        stream = StreamInfo(
            id="101",
            channel_id=1,
            type_id=1,
            name="Main",
            enabled=True,
        )
        camera = CameraInfo(
            id=1,
            name="Camera 1",
            streams=[stream],
        )
        self.assertEqual(camera.id, 1)
        self.assertEqual(len(camera.streams), 1)

    def test_device_capabilities(self):
        """Test DeviceCapabilities dataclass."""
        caps = DeviceCapabilities(
            support_holiday_mode=True,
            support_io_outputs=True,
            num_io_outputs=2,
        )
        self.assertTrue(caps.support_holiday_mode)
        self.assertEqual(caps.num_io_outputs, 2)


if __name__ == "__main__":
    unittest.main()
