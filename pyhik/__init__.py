"""pyhik - Python library for Hikvision camera/NVR events."""

from pyhik.hikvision import HikCamera, get_nvr_events, inject_events_into_camera
from pyhik.constants import __version__

__all__ = ['HikCamera', 'get_nvr_events', 'inject_events_into_camera', '__version__']
