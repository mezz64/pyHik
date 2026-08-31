[![PyPI](https://img.shields.io/pypi/v/pyHik.svg)](https://pypi.python.org/pypi/pyHik)

# Introduction

This is a python module for interacting with Hikvision IP cameras and NVRs. Most rebadged models work as well with full functionality. It provides two ways to talk to a device:

* **`HikCamera`** - connects to a device's ISAPI event stream and tracks event/sensor state changes in real time (motion, line crossing, tamper, I/O, disk errors, etc.), with callbacks on updates. Also supports snapshots, RTSP stream URLs, motion detection toggling, and recording search/download.
* **`ISAPIClient`** - a synchronous ISAPI client for querying and controlling a device: device info/capabilities, storage devices, streaming channels, cameras, I/O ports, event enable/disable, snapshots, RTSP URLs, holiday mode, and reboot.

Code is licensed under the MIT license.

# Requirements

* Python 3.9+
* [requests](https://pypi.org/project/requests/) >= 2.20.0

Optional:
* `xmltodict` >= 0.13.0 - required for `ISAPIClient` (install with the `isapi` extra)
* [pyDispatcher](https://pypi.org/project/PyDispatcher/) 2.0.5 - only needed if you want `HikCamera` to also broadcast updates via `pydispatch` signals; the built-in callback mechanism (`add_update_callback`) works without it

# Installation

```
pip install pyhik
```

To also use `ISAPIClient`:

```
pip install pyhik[isapi]
```

# Usage - Event streaming (`HikCamera`)

```python
import pyhik.hikvision as hikvision

camera = hikvision.HikCamera('http://X.X.X.X', port=80, usr='admin', pwd='1234')

# Register a callback, then start the event stream processing thread
camera.add_update_callback(lambda msg: print('Update:', msg), camera.get_id)
camera.start_stream()

print(camera.get_name, camera.current_event_states)

# ...when finished
camera.disconnect()
```

### Callbacks
* `add_update_callback(callback, msg)` - register a callback function, called with `cam_id.event_type.channel` whenever a tracked event changes state.

### Properties
* `get_id` - unique camera/NVR id
* `get_name` - camera/NVR name
* `get_type` - device type (`CAM` or `NVR`)
* `current_event_states` - dictionary of all tracked events and their current state
* `current_motion_detection_state` - current motion detection on/off state
* `stream_connected` - whether the event stream is currently connected

### Functions
* `start_stream()` - start the event stream processing thread
* `disconnect()` - close the event stream session and stop the processing thread
* `get_channels()` - list available channels
* `get_motion_detection()` / `enable_motion_detection()` / `disable_motion_detection()` - read/toggle motion detection
* `get_snapshot(channel=1)` - fetch a JPEG snapshot for a channel
* `get_stream_url(channel=1, protocol='rtsp', stream_type=1)` - build an RTSP stream URL
* `fetch_attributes(event, channel)` - get the state list for a specific sensor/channel
* `get_recording_days(track_id, start_date, end_date)` - list days that have recordings
* `search_recordings(track_id, start_time, end_time, max_results=100)` - search for recordings in a time range
* `download_recording(playback_uri, output_stream=None, chunk_size=65536)` - download a recording found via `search_recordings`

# Usage - Device control (`ISAPIClient`)

```python
from pyhik.isapi import ISAPIClient

with ISAPIClient('X.X.X.X', port=80, username='admin', password='1234') as client:
    print(client.get_device_name(), client.get_firmware_version())
    print(client.get_capabilities())

    snapshot = client.get_snapshot(channel=1)
    rtsp_url = client.get_rtsp_url(channel=1)
```

### Functions
* `get_device_info()` / `get_device_serial()` / `get_device_name()` / `get_device_model()` / `get_device_type()` / `get_firmware_version()` - device identity
* `get_capabilities()` - returns a `DeviceCapabilities` summary of what the device supports
* `get_storage_devices()` - list `StorageDevice` entries (HDD/SD status, capacity)
* `get_alarm_server_info()` / configure via `request()` - alarm server settings
* `get_streaming_channels()` - list `StreamInfo` for available streams
* `get_cameras()` - list `CameraInfo` (useful on NVRs with multiple channels)
* `get_output_ports()` / `get_input_ports()` - list I/O ports
* `get_output_state(output_id)` / `set_output_state(output_id, state)` - read/set an output relay
* `get_holiday_mode_enabled()` / `set_holiday_mode_enabled(enabled)` - read/set holiday mode
* `get_event_states()` / `set_event_enabled(...)` - read/toggle event detection types
* `get_snapshot(channel=1, stream_type=1, width=None, height=None)` - fetch a snapshot
* `get_rtsp_url(channel=1, stream_type=1, include_credentials=True)` - build an RTSP stream URL
* `reboot()` - reboot the device
* `custom_request(...)` - issue an arbitrary ISAPI request for endpoints not otherwise wrapped

# TODO

* Support IR day/night status and ability to switch between day/night/auto
