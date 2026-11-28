[![PyPI](https://img.shields.io/pypi/v/pyHik.svg)](https://pypi.python.org/pypi/pyHik)

# Introduction

This is a python module for interacting with Hikvision IP cameras and NVRs. Most rebadged models work as well with full functionality. It provides two ways to talk to a device:

* **`HikCamera`** - connects to a device's ISAPI event stream and tracks event/sensor state changes in real time (motion, line crossing, tamper, I/O, disk errors, etc.), with callbacks on updates. Also supports snapshots, RTSP stream URLs, motion detection toggling, and recording search/download.
* **`ISAPIClient`** - a synchronous ISAPI client for querying and controlling a device: device info/capabilities, storage devices, streaming channels, cameras, I/O ports, event enable/disable, snapshots, RTSP URLs, holiday mode, and reboot.

Code is licensed under the MIT license.

# Requirements

* Python 3.10+
* [requests](https://pypi.org/project/requests/) >= 2.20.0
* [xmltodict](https://pypi.org/project/xmltodict/) >= 0.13.0 - used by `ISAPIClient`

Optional:
* [pyDispatcher](https://pypi.org/project/PyDispatcher/) 2.0.5 - only needed if you want `HikCamera` to also broadcast updates via `pydispatch` signals; the built-in callback mechanism (`add_update_callback`) works without it

# Installation

```
pip install pyhik
```

`ISAPIClient` is included. (`pip install pyhik[isapi]` still works, but the extra is no longer needed.)

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
* `auth_failed` - `True` if the device answered but rejected the credentials, so a missing `get_id` can be told apart from an unreachable device
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
* `request_raw(method, endpoint, body=None)` - like `custom_request` but sends the body unmodified and returns the unparsed response (`RawResponse`: status code, bytes, case-insensitive headers) without raising on HTTP error statuses (after a 401 the request is retried once with Digest auth)
* `get_image_settings(channel=1, proxy=False)` - list the image settings a camera supports (shutter, gain, WDR, BLC, day/night, ...) with their current values (`ImageSetting` objects, see `pyhik.image`)
* `get_image_values(channel=1, proxy=False)` - read only the current values as `{path: value}`; cheap enough to poll
* `set_image_setting(path, value, channel=1, proxy=False)` - change one image setting; known conflicts between settings are resolved automatically
* `set_image_linked_setting(setting, value, channel=1, proxy=False)` - set a select that also controls an `enabled` flag, such as BLC mode
* `put_image_settings(changes, channel=1, proxy=False)` - lower level: write several `{path: value}` changes in one request, without conflict handling
* `get_image_channels(proxy=False)` - list the channel IDs that have image settings (`ImageChannelInfo`)

### Image settings

Cameras expose their image settings (shutter, gain, WDR, BLC, day/night, supplement light, ...) under `/ISAPI/Image/channels/<n>`. Which settings exist differs per model, so they are discovered from the device:

```python
client = ISAPIClient("192.168.1.100", username="admin", password="secret")
settings = {s.path: s for s in client.get_image_settings()}
settings["Color/brightnessLevel"].value    # '80'
settings["Shutter/ShutterLevel"].options   # ['1/3', '1/6', ..., '1/100000']
```

Reading raises:

* `ISAPINotSupportedError` (a subclass of `ISAPINotFoundError`) if the device does not offer the endpoint (404, 501 or a `notSupport` reply)
* `ISAPIAuthError` for credential or permission problems
* `ISAPIConnectionError` if the device cannot be reached

These exceptions carry `status_code` and `sub_status`, the HTTP status and the device's `subStatusCode`.

Changing a setting:

```python
result = client.set_image_setting("Shutter/ShutterLevel", "1/500")
result.success, result.sub_status          # True, 'ok'
```

#### Write Mechanism

* **Read-Modify-Write:** A write operation reads the full document, changes only the target value as text, and sends the updated document back to the device.
* **Non-Atomic Operations:** Writes are **not atomic**—avoid overlapping or concurrent writes to the same device.

#### Conflict Resolution

Some settings inherently conflict with one another (e.g. enabling WDR while BLC or HLC is active and vice versa). When a conflict occurs:

* **Automatic Override:** The conflicting setting is automatically switched off first to make room for the new setting.
* **Firmware Error Codes:** Firmware reporting conflicts as `HLCMutexWDROrDehaze` or `WDROrDehazeMutexHLC` (HTTP 403) is handled identically:
* HLC is switched off when WDR or defog is turned on.
* WDR and defog are switched off when HLC is turned on.

* **Result Tracking:**
* `ImageWriteResult.also_changed` lists the settings that were automatically disabled (e.g. `{"HLC/enabled": "false"}`).
* `ImageWriteResult.error_description` explains any conflicts that could not be resolved.

#### Fallback Handling (Sub-Endpoints)

* **`notSupport` Handling:** If a camera rejects writes to the full document with a `notSupport` error, the system automatically attempts to use per-feature sub-endpoints (example `/ISAPI/Image/channels/1/Shutter`).
* **Condition:** This fallback only triggers when all requested changes belong to the **same feature** (such as `BLC/enabled` and `BLC/BLCMode`).

For the cameras attached to an NVR, pass `proxy=True` (`/ISAPI/ContentMgmt/ImageProxy/channels/<id>`) and use the IDs returned by `get_image_channels(proxy=True)`:

```python
for channel in client.get_image_channels(proxy=True):
    settings = client.get_image_settings(channel.id, proxy=True)
```

* The sub-endpoint fallback is not used through the NVR.
* An NVR may list a channel whose camera it cannot proxy; reading it raises `ISAPINotSupportedError`.

Recorders do not always report what they are; be ready to fall back to the NVR endpoints:

* A recorder may report `deviceType` `IPC`, the type a camera reports; the type alone cannot tell an NVR from a camera.
* If the direct listing fails, try `get_image_channels(proxy=True)`.
* An NVR may reject the camera endpoints (`/ISAPI/Image/channels`) with HTTP 503 `serviceUnavailable` or 400 `badXmlContent`. pyhik raises a plain `ISAPIError` for these; catching only `ISAPINotSupportedError` will not catch them.
* `ISAPINotSupportedError` only means that the device says it does not offer the endpoint: HTTP 404, 501 or a `notSupport` reply.
* For any other error, check `status_code` and `sub_status` on the exception.

### Checking the ISAPI image settings

`python -m pyhik checkisapi` tries the image settings code against a real camera or NVR.
[TESTING.md](TESTING.md) has the instructions to use it and can be used to report a
problem.

# TODO

* Support IR day/night status and ability to switch between day/night/auto
