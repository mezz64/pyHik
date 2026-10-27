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
    ISAPINotSupportedError,
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
from pyhik.image import (
    ImageSetting,
    ImageSettingType,
    parse_image_capabilities,
    parse_image_values,
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
    'ISAPINotSupportedError',
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
    # Image settings
    'ImageSetting',
    'ImageSettingType',
    'parse_image_capabilities',
    'parse_image_values',
]
