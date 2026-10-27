"""pyhik - Python library for Hikvision camera/NVR events."""

from pyhik.hikvision import (
    HikCamera,
    inject_events_into_camera,
    get_video_channels,
    Recording,
    RecordingDay,
    VideoChannel,
)
from pyhik.constants import __version__, VALID_NOTIFICATION_METHODS
from pyhik.isapi import (
    ISAPIClient,
    ISAPIError,
    ISAPIConnectionError,
    ISAPIAuthError,
    ISAPINotFoundError,
    RawResponse,
    StorageDevice,
    AlarmServerInfo,
    StreamInfo,
    CameraInfo,
    OutputPort,
    InputPort,
    EventState,
    DeviceCapabilities,
)

__all__ = [
    # Legacy event-based API
    'HikCamera',
    'inject_events_into_camera',
    'get_video_channels',
    'Recording',
    'RecordingDay',
    'VideoChannel',
    'VALID_NOTIFICATION_METHODS',
    '__version__',
    # ISAPI client
    'ISAPIClient',
    'ISAPIError',
    'ISAPIConnectionError',
    'ISAPIAuthError',
    'ISAPINotFoundError',
    'RawResponse',
    # Data classes
    'StorageDevice',
    'AlarmServerInfo',
    'StreamInfo',
    'CameraInfo',
    'OutputPort',
    'InputPort',
    'EventState',
    'DeviceCapabilities',
]
