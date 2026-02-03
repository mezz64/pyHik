"""
pyhik.isapi
~~~~~~~~~~~
ISAPI client for Hikvision devices.
Provides comprehensive access to Hikvision ISAPI endpoints.

Copyright (c) 2016-2026 John Mihalic <https://github.com/mezz64>
Licensed under the MIT license.
"""

from dataclasses import dataclass, field
from enum import Enum
import logging
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote, urlparse, urlunparse

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

try:
    import xmltodict
except ImportError:
    xmltodict = None

_LOGGER = logging.getLogger(__name__)

# ISAPI Endpoints
ENDPOINT_DEVICE_INFO = "/ISAPI/System/deviceInfo"
ENDPOINT_CAPABILITIES = "/ISAPI/System/capabilities"
ENDPOINT_STORAGE = "/ISAPI/ContentMgmt/Storage"
ENDPOINT_STREAMING_CHANNELS = "/ISAPI/Streaming/channels"
ENDPOINT_INPUT_PROXY_CHANNELS = "/ISAPI/ContentMgmt/InputProxy/channels"
ENDPOINT_IO_INPUTS = "/ISAPI/System/IO/inputs"
ENDPOINT_IO_OUTPUTS = "/ISAPI/System/IO/outputs"
ENDPOINT_EVENT_NOTIFICATION = "/ISAPI/Event/notification/httpHosts"
ENDPOINT_HOLIDAYS = "/ISAPI/System/Holidays"
ENDPOINT_REBOOT = "/ISAPI/System/reboot"
ENDPOINT_EVENT_TRIGGERS = "/ISAPI/Event/triggers"
ENDPOINT_SMART_CAPABILITIES = "/ISAPI/Smart/capabilities"

# Event detection endpoints
EVENT_ENDPOINTS: Dict[str, str] = {
    "motionDetection": "/ISAPI/System/Video/inputs/channels/{channel}/motionDetection",
    "lineDetection": "/ISAPI/Smart/LineDetection/{channel}",
    "fieldDetection": "/ISAPI/Smart/FieldDetection/{channel}",
    "regionEntrance": "/ISAPI/Smart/RegionEntrance/{channel}",
    "regionExiting": "/ISAPI/Smart/RegionExiting/{channel}",
    "tamperDetection": "/ISAPI/System/Video/inputs/channels/{channel}/tamperDetection",
    "sceneChangeDetection": "/ISAPI/Smart/SceneChangeDetection/{channel}",
    "PIR": "/ISAPI/WLAlarm/PIR",
    "faceDetection": "/ISAPI/Smart/FaceDetect/{channel}",
}

# Request timeout
REQUEST_TIMEOUT = 20


class HTTPMethod(Enum):
    """HTTP methods."""

    GET = "GET"
    PUT = "PUT"
    POST = "POST"
    DELETE = "DELETE"


class ISAPIError(Exception):
    """Base ISAPI error."""


class ISAPIConnectionError(ISAPIError):
    """Connection error."""


class ISAPIAuthError(ISAPIError):
    """Authentication error."""


class ISAPINotFoundError(ISAPIError):
    """Resource not found error."""


@dataclass
class StorageDevice:
    """Storage device information."""

    id: str
    name: str
    status: str
    type: str
    capacity: Optional[int] = None
    free_space: Optional[int] = None
    ip_address: Optional[str] = None


@dataclass
class AlarmServerInfo:
    """Alarm server configuration."""

    protocol: str = ""
    address: str = ""
    port: int = 0
    path: str = ""


@dataclass
class StreamInfo:
    """Camera stream information."""

    id: str
    channel_id: int
    type_id: int
    name: str
    enabled: bool


@dataclass
class CameraInfo:
    """Camera information."""

    id: int
    name: str
    model: Optional[str] = None
    serial_number: Optional[str] = None
    input_port: Optional[int] = None
    streams: List["StreamInfo"] = field(default_factory=list)


@dataclass
class OutputPort:
    """Output port information."""

    id: str
    name: str


@dataclass
class InputPort:
    """Input port information."""

    id: str
    name: str


@dataclass
class EventState:
    """Event detection state."""

    id: str
    channel: int
    type: str
    enabled: bool


@dataclass
class DeviceCapabilities:
    """Device capabilities."""

    support_holiday_mode: bool = False
    support_alarm_server: bool = False
    support_io_outputs: bool = False
    support_io_inputs: bool = False
    support_storage: bool = False
    num_io_outputs: int = 0
    num_io_inputs: int = 0


class ISAPIClient:
    """Client for Hikvision ISAPI.

    Provides synchronous access to Hikvision device ISAPI endpoints.
    """

    def __init__(
        self,
        host: str,
        port: int = 80,
        username: str = "",
        password: str = "",
        ssl: bool = False,
        verify_ssl: bool = True,
        rtsp_port: int = 554,
    ) -> None:
        """Initialize the ISAPI client.

        Args:
            host: Device hostname or IP address.
            port: HTTP port (default 80).
            username: Authentication username.
            password: Authentication password.
            ssl: Use HTTPS if True.
            verify_ssl: Verify SSL certificates.
            rtsp_port: RTSP port for streaming (default 554).
        """
        # Parse the host to extract clean hostname and handle URLs with scheme/port
        protocol = "https" if ssl else "http"
        parsed = urlparse(host if '://' in str(host) else f'{protocol}://{host}')
        self.host = parsed.hostname or host
        self.port = parsed.port or port
        self.username = username
        self.password = password
        self.ssl = ssl
        self.verify_ssl = verify_ssl
        self.rtsp_port = rtsp_port

        self.base_url = urlunparse((
            protocol, f'{self.host}:{self.port}', '', '', '', ''
        ))

        self._session = requests.Session()
        self._session.verify = verify_ssl
        self._auth: Optional[Union[HTTPBasicAuth, HTTPDigestAuth]] = None
        self._device_info: Dict[str, Any] = {}
        self._capabilities: Optional[DeviceCapabilities] = None

    def _detect_auth_method(self) -> None:
        """Detect the authentication method (Basic or Digest)."""
        if self._auth is not None:
            return

        url = f"{self.base_url}{ENDPOINT_DEVICE_INFO}"

        # Try digest auth first (more common for Hikvision)
        try:
            digest_auth = HTTPDigestAuth(self.username, self.password)
            response = self._session.get(url, auth=digest_auth, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                self._auth = digest_auth
                return
        except Exception:
            pass

        # Fall back to basic auth
        self._auth = HTTPBasicAuth(self.username, self.password)

    def _parse_xml(self, text: str) -> Dict[str, Any]:
        """Parse XML response to dictionary."""
        if xmltodict is None:
            raise ISAPIError(
                "xmltodict is required for ISAPI client. "
                "Install it with: pip install xmltodict"
            )
        try:
            return xmltodict.parse(text)
        except Exception:
            return {"raw": text}

    def _unparse_xml(self, data: Dict[str, Any]) -> str:
        """Convert dictionary to XML string."""
        if xmltodict is None:
            raise ISAPIError(
                "xmltodict is required for ISAPI client. "
                "Install it with: pip install xmltodict"
            )
        return xmltodict.unparse(data)

    def request(
        self,
        method: HTTPMethod,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Union[Dict[str, Any], bytes]:
        """Make an ISAPI request.

        Args:
            method: HTTP method to use.
            endpoint: ISAPI endpoint path.
            data: Optional data to send (will be converted to XML).
            params: Optional query parameters.

        Returns:
            Parsed XML response as dictionary, or raw bytes for binary content.

        Raises:
            ISAPIConnectionError: Connection failed.
            ISAPIAuthError: Authentication failed.
            ISAPINotFoundError: Endpoint not found.
            ISAPIError: Other request errors.
        """
        self._detect_auth_method()

        url = f"{self.base_url}{endpoint}"

        try:
            if method == HTTPMethod.GET:
                response = self._session.get(
                    url, auth=self._auth, params=params, timeout=REQUEST_TIMEOUT
                )
            elif method == HTTPMethod.PUT:
                xml_data = self._unparse_xml(data) if data else None
                response = self._session.put(
                    url,
                    auth=self._auth,
                    data=xml_data,
                    headers={"Content-Type": "application/xml"},
                    timeout=REQUEST_TIMEOUT,
                )
            elif method == HTTPMethod.POST:
                xml_data = self._unparse_xml(data) if data else None
                response = self._session.post(
                    url,
                    auth=self._auth,
                    data=xml_data,
                    headers={"Content-Type": "application/xml"},
                    timeout=REQUEST_TIMEOUT,
                )
            elif method == HTTPMethod.DELETE:
                response = self._session.delete(
                    url, auth=self._auth, timeout=REQUEST_TIMEOUT
                )
            else:
                response = self._session.request(
                    method.value, url, auth=self._auth, timeout=REQUEST_TIMEOUT
                )

        except requests.exceptions.ConnectionError as err:
            raise ISAPIConnectionError(f"Cannot connect to {self.host}") from err
        except requests.exceptions.Timeout as err:
            raise ISAPIConnectionError(f"Timeout connecting to {self.host}") from err
        except requests.exceptions.RequestException as err:
            raise ISAPIConnectionError(f"Request failed: {err}") from err

        if response.status_code == 401:
            raise ISAPIAuthError("Invalid credentials")
        if response.status_code == 403:
            raise ISAPIAuthError("Insufficient permissions")
        if response.status_code == 404:
            raise ISAPINotFoundError(f"Endpoint not found: {endpoint}")
        if response.status_code >= 400:
            raise ISAPIError(f"Request failed with status {response.status_code}")

        content_type = response.headers.get("content-type", "")
        if "image" in content_type or "octet-stream" in content_type:
            return response.content

        return self._parse_xml(response.text)

    def get_device_info(self) -> Dict[str, Any]:
        """Get device information."""
        if not self._device_info:
            response = self.request(HTTPMethod.GET, ENDPOINT_DEVICE_INFO)
            self._device_info = response.get("DeviceInfo", {})
        return self._device_info

    def get_device_serial(self) -> str:
        """Get device serial number."""
        info = self.get_device_info()
        return info.get("serialNumber", "")

    def get_device_name(self) -> str:
        """Get device name."""
        info = self.get_device_info()
        return info.get("deviceName", "")

    def get_device_model(self) -> str:
        """Get device model."""
        info = self.get_device_info()
        return info.get("model", "")

    def get_device_type(self) -> str:
        """Get device type (e.g., NVR, DVR, IPCamera)."""
        info = self.get_device_info()
        return info.get("deviceType", "Camera")

    def get_firmware_version(self) -> str:
        """Get device firmware version."""
        info = self.get_device_info()
        return info.get("firmwareVersion", "")

    def get_capabilities(self) -> DeviceCapabilities:
        """Get device capabilities."""
        if self._capabilities is not None:
            return self._capabilities

        capabilities = DeviceCapabilities()

        # Check for holiday mode support
        try:
            self.request(HTTPMethod.GET, ENDPOINT_HOLIDAYS)
            capabilities.support_holiday_mode = True
        except (ISAPINotFoundError, ISAPIError):
            pass

        # Check for alarm server support
        try:
            self.request(HTTPMethod.GET, ENDPOINT_EVENT_NOTIFICATION)
            capabilities.support_alarm_server = True
        except (ISAPINotFoundError, ISAPIError):
            pass

        # Check for IO outputs
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_IO_OUTPUTS)
            outputs = response.get("IOOutputPortList", {}).get("IOOutputPort", [])
            if isinstance(outputs, dict):
                outputs = [outputs]
            capabilities.support_io_outputs = len(outputs) > 0
            capabilities.num_io_outputs = len(outputs)
        except (ISAPINotFoundError, ISAPIError):
            pass

        # Check for IO inputs
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_IO_INPUTS)
            inputs = response.get("IOInputPortList", {}).get("IOInputPort", [])
            if isinstance(inputs, dict):
                inputs = [inputs]
            capabilities.support_io_inputs = len(inputs) > 0
            capabilities.num_io_inputs = len(inputs)
        except (ISAPINotFoundError, ISAPIError):
            pass

        # Check for storage
        try:
            self.request(HTTPMethod.GET, ENDPOINT_STORAGE)
            capabilities.support_storage = True
        except (ISAPINotFoundError, ISAPIError):
            pass

        self._capabilities = capabilities
        return capabilities

    def get_storage_devices(self) -> List[StorageDevice]:
        """Get storage device information."""
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_STORAGE)
        except ISAPINotFoundError:
            return []

        devices = []
        storage_list = response.get("storage") or {}

        # Handle HDD list
        hdd_list = (storage_list.get("hddList") or {}).get("hdd", [])
        if isinstance(hdd_list, dict):
            hdd_list = [hdd_list]

        for hdd in hdd_list:
            devices.append(
                StorageDevice(
                    id=hdd.get("id", ""),
                    name=hdd.get("hddName", f"HDD {hdd.get('id', '')}"),
                    status=hdd.get("status", "unknown"),
                    type="HDD",
                    capacity=self._parse_capacity(hdd.get("capacity")),
                    free_space=self._parse_capacity(hdd.get("freeSpace")),
                )
            )

        # Handle NAS list
        nas_list = (storage_list.get("nasList") or {}).get("nas", [])
        if isinstance(nas_list, dict):
            nas_list = [nas_list]

        for nas in nas_list:
            devices.append(
                StorageDevice(
                    id=nas.get("id", ""),
                    name=nas.get("nasName", f"NAS {nas.get('id', '')}"),
                    status=nas.get("status", "unknown"),
                    type="NAS",
                    capacity=self._parse_capacity(nas.get("capacity")),
                    free_space=self._parse_capacity(nas.get("freeSpace")),
                    ip_address=nas.get("ipAddress"),
                )
            )

        return devices

    def _parse_capacity(self, value: Optional[str]) -> Optional[int]:
        """Parse capacity value to bytes.

        Hikvision returns storage values in MB, convert to bytes.
        """
        if value is None:
            return None
        try:
            return int(value) * 1024 * 1024
        except (ValueError, TypeError):
            return None

    def get_alarm_server_info(self) -> AlarmServerInfo:
        """Get alarm server configuration."""
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_EVENT_NOTIFICATION)
        except ISAPINotFoundError:
            return AlarmServerInfo()

        hosts = response.get("HttpHostNotificationList", {}).get(
            "HttpHostNotification", []
        )
        if isinstance(hosts, dict):
            hosts = [hosts]

        if not hosts:
            return AlarmServerInfo()

        host = hosts[0]
        return AlarmServerInfo(
            protocol=host.get("protocolType", ""),
            address=host.get("ipAddress", host.get("hostName", "")),
            port=int(host.get("portNo", 0)),
            path=host.get("url", ""),
        )

    def get_streaming_channels(self) -> List[StreamInfo]:
        """Get streaming channel information."""
        # Try standard streaming channels endpoint first
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_STREAMING_CHANNELS)
            channels = response.get("StreamingChannelList", {}).get(
                "StreamingChannel", []
            )
            if isinstance(channels, dict):
                channels = [channels]

            streams = []
            for channel in channels:
                channel_id = channel.get("id", "")
                try:
                    full_id = int(channel_id)
                    cam_id = full_id // 100
                    stream_type = full_id % 100
                except (ValueError, TypeError):
                    continue

                streams.append(
                    StreamInfo(
                        id=channel_id,
                        channel_id=cam_id,
                        type_id=stream_type,
                        name=channel.get("channelName", f"Channel {cam_id}"),
                        enabled=channel.get("enabled", "true").lower() == "true",
                    )
                )

            if streams:
                return streams
        except (ISAPINotFoundError, ISAPIError):
            pass

        # Try NVR input proxy channels endpoint
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_INPUT_PROXY_CHANNELS)
            channels = response.get("InputProxyChannelList", {}).get(
                "InputProxyChannel", []
            )
            if isinstance(channels, dict):
                channels = [channels]

            streams = []
            for channel in channels:
                try:
                    channel_id = int(channel.get("id", 0))
                except (ValueError, TypeError):
                    continue

                channel_name = channel.get("name", f"Channel {channel_id}")
                # Create main stream (type 1) for each NVR channel
                streams.append(
                    StreamInfo(
                        id=f"{channel_id}01",
                        channel_id=channel_id,
                        type_id=1,
                        name=channel_name,
                        enabled=True,
                    )
                )
                # Create sub stream (type 2) for each NVR channel
                streams.append(
                    StreamInfo(
                        id=f"{channel_id}02",
                        channel_id=channel_id,
                        type_id=2,
                        name=channel_name,
                        enabled=True,
                    )
                )

            return streams
        except (ISAPINotFoundError, ISAPIError):
            return []

    def get_cameras(self) -> List[CameraInfo]:
        """Get camera information with streams."""
        streams = self.get_streaming_channels()

        cameras_dict: Dict[int, CameraInfo] = {}
        for stream in streams:
            if stream.channel_id not in cameras_dict:
                cameras_dict[stream.channel_id] = CameraInfo(
                    id=stream.channel_id,
                    name=stream.name,
                    streams=[],
                )
            cameras_dict[stream.channel_id].streams.append(stream)

        return list(cameras_dict.values())

    def get_output_ports(self) -> List[OutputPort]:
        """Get output port information."""
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_IO_OUTPUTS)
        except ISAPINotFoundError:
            return []

        outputs = response.get("IOOutputPortList", {}).get("IOOutputPort", [])
        if isinstance(outputs, dict):
            outputs = [outputs]

        return [
            OutputPort(
                id=output.get("id", ""),
                name=output.get("outputName", f"Output {output.get('id', '')}"),
            )
            for output in outputs
        ]

    def get_input_ports(self) -> List[InputPort]:
        """Get input port information."""
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_IO_INPUTS)
        except ISAPINotFoundError:
            return []

        inputs = response.get("IOInputPortList", {}).get("IOInputPort", [])
        if isinstance(inputs, dict):
            inputs = [inputs]

        return [
            InputPort(
                id=inp.get("id", ""),
                name=inp.get("inputName", f"Input {inp.get('id', '')}"),
            )
            for inp in inputs
        ]

    def get_output_state(self, output_id: str) -> bool:
        """Get output port state."""
        try:
            response = self.request(
                HTTPMethod.GET, f"{ENDPOINT_IO_OUTPUTS}/{output_id}/status"
            )
            status = response.get("IOPortStatus", {})
            return status.get("ioState", "inactive").lower() == "active"
        except ISAPIError:
            return False

    def set_output_state(self, output_id: str, state: bool) -> None:
        """Set output port state."""
        data = {
            "IOPortData": {
                "@version": "2.0",
                "@xmlns": "http://www.isapi.org/ver20/XMLSchema",
                "outputState": "high" if state else "low",
            }
        }
        self.request(
            HTTPMethod.PUT, f"{ENDPOINT_IO_OUTPUTS}/{output_id}/trigger", data=data
        )

    def get_holiday_mode_enabled(self) -> bool:
        """Get holiday mode status."""
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_HOLIDAYS)
            holidays = response.get("HolidayList", {}).get("holiday", [])
            if isinstance(holidays, dict):
                holidays = [holidays]
            for h in holidays:
                enabled = h.get("enabled", "false")
                if isinstance(enabled, dict):
                    enabled = enabled.get("#text", "false")
                if str(enabled).lower() == "true":
                    return True
            return False
        except ISAPIError:
            return False

    def set_holiday_mode_enabled(self, enabled: bool) -> None:
        """Set holiday mode status."""
        try:
            response = self.request(HTTPMethod.GET, ENDPOINT_HOLIDAYS)
        except ISAPIError:
            return

        holidays = response.get("HolidayList", {}).get("holiday", [])
        if isinstance(holidays, dict):
            holidays = [holidays]

        if not holidays:
            return

        holidays[0]["enabled"] = "true" if enabled else "false"
        data = {"HolidayList": {"holiday": holidays}}
        self.request(HTTPMethod.PUT, ENDPOINT_HOLIDAYS, data=data)

    def get_event_states(self) -> List[EventState]:
        """Get event detection states for all channels."""
        states = []
        cameras = self.get_cameras()

        for camera in cameras:
            channel = camera.id
            for event_type, endpoint_template in EVENT_ENDPOINTS.items():
                endpoint = endpoint_template.format(channel=channel)
                try:
                    response = self.request(HTTPMethod.GET, endpoint)
                    for value in response.values():
                        if isinstance(value, dict) and "enabled" in value:
                            enabled = value.get("enabled", "false").lower() == "true"
                            states.append(
                                EventState(
                                    id=f"{event_type}_{channel}",
                                    channel=channel,
                                    type=event_type,
                                    enabled=enabled,
                                )
                            )
                            break
                except ISAPIError:
                    continue

        return states

    def set_event_enabled(
        self, event_type: str, channel: int, enabled: bool
    ) -> None:
        """Set event detection enabled state."""
        endpoint_template = EVENT_ENDPOINTS.get(event_type)
        if not endpoint_template:
            raise ISAPIError(f"Unknown event type: {event_type}")

        endpoint = endpoint_template.format(channel=channel)

        try:
            response = self.request(HTTPMethod.GET, endpoint)
        except ISAPIError as err:
            raise ISAPIError(f"Cannot get event config: {err}") from err

        for value in response.values():
            if isinstance(value, dict) and "enabled" in value:
                value["enabled"] = "true" if enabled else "false"
                break

        self.request(HTTPMethod.PUT, endpoint, data=response)

    def get_snapshot(
        self,
        channel: int = 1,
        stream_type: int = 1,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> bytes:
        """Get camera snapshot.

        Args:
            channel: Camera channel number (default 1).
            stream_type: Stream type (1=main, 2=sub, default 1).
            width: Optional image width.
            height: Optional image height.

        Returns:
            Image data as bytes.
        """
        stream_id = channel * 100 + stream_type
        endpoint = f"{ENDPOINT_STREAMING_CHANNELS}/{stream_id}/picture"

        params = {}
        if width:
            params["width"] = width
        if height:
            params["height"] = height

        result = self.request(HTTPMethod.GET, endpoint, params=params or None)
        if isinstance(result, bytes):
            return result
        raise ISAPIError("Failed to get snapshot")

    def get_rtsp_url(
        self,
        channel: int = 1,
        stream_type: int = 1,
        include_credentials: bool = True,
    ) -> str:
        """Get RTSP URL for a channel.

        Args:
            channel: Camera channel number (default 1).
            stream_type: Stream type (1=main, 2=sub, default 1).
            include_credentials: Include username/password in URL.

        Returns:
            RTSP URL string.
        """
        stream_id = channel * 100 + stream_type
        protocol = "rtsps" if self.ssl else "rtsp"
        path = f"/Streaming/Channels/{stream_id}"

        if include_credentials:
            encoded_user = quote(self.username, safe='')
            encoded_pwd = quote(self.password, safe='')
            return urlunparse((
                protocol,
                f'{encoded_user}:{encoded_pwd}@{self.host}:{self.rtsp_port}',
                path, '', '', ''
            ))
        return urlunparse((
            protocol,
            f'{self.host}:{self.rtsp_port}',
            path, '', '', ''
        ))

    def reboot(self) -> None:
        """Reboot the device."""
        self.request(HTTPMethod.PUT, ENDPOINT_REBOOT)

    def custom_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make a custom ISAPI request.

        Args:
            method: HTTP method (GET, PUT, POST, DELETE).
            endpoint: ISAPI endpoint path.
            data: Optional XML data string.

        Returns:
            Parsed response as dictionary.
        """
        http_method = HTTPMethod(method.upper())

        if data:
            try:
                parsed_data = self._parse_xml(data)
            except Exception as err:
                raise ISAPIError(f"Invalid XML data: {err}") from err
        else:
            parsed_data = None

        result = self.request(http_method, endpoint, data=parsed_data)
        if isinstance(result, bytes):
            return {"raw_bytes": True, "length": len(result)}
        return result

    def close(self) -> None:
        """Close the client session."""
        self._session.close()

    def __enter__(self) -> "ISAPIClient":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()
