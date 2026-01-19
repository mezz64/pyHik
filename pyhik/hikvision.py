"""
pyhik.hikvision
~~~~~~~~~~~~~~~~~~~~
Provides api for Hikvision events
Copyright (c) 2016-2021 John Mihalic <https://github.com/mezz64>
Licensed under the MIT license.

Based on the following api documentation:
System:
http://oversea-download.hikvision.com/uploadfile/Leaflet/ISAPI/HIKVISION%20ISAPI_2.0-IPMD%20Service.pdf
Imaging:
http://oversea-download.hikvision.com/uploadfile/Leaflet/ISAPI/HIKVISION%20ISAPI_2.0-Image%20Service.pdf
"""
import time
import datetime
from dataclasses import dataclass
import logging
import uuid
from urllib.parse import quote

try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

import threading
import requests
from requests.auth import HTTPDigestAuth

# Make pydispatcher optional to support legacy implentations
# New usage should implement the event_callback
try:
    from pydispatch import dispatcher
except ImportError:
    dispatcher = None

from pyhik.watchdog import Watchdog
from pyhik.constants import (
    DEFAULT_PORT, DEFAULT_RTSP_PORT, DEFAULT_HEADERS, XML_NAMESPACE, SENSOR_MAP,
    CAM_DEVICE, NVR_DEVICE, CONNECT_TIMEOUT, READ_TIMEOUT, SNAPSHOT_TIMEOUT,
    RECORDING_SEARCH_TIMEOUT, CONTEXT_INFO, CONTEXT_TRIG, CONTEXT_MOTION,
    CONTEXT_ALERT, CHANNEL_NAMES, ID_TYPES, __version__)

# Register the default namespace to avoid ns0: prefixes in serialized XML
ET.register_namespace('', XML_NAMESPACE)


_LOGGING = logging.getLogger(__name__)


@dataclass
class Recording:
    """Represents a recording from the Hikvision device."""

    source_id: str
    track_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    content_type: str
    playback_uri: str


@dataclass
class RecordingDay:
    """Represents a day with recordings available."""

    date: datetime.datetime
    has_recordings: bool


@dataclass
class VideoChannel:
    """Represents a video input channel on a Hikvision device."""

    id: int
    name: str
    enabled: bool = True

# Hide nuisance requests logging
logging.getLogger('urllib3').setLevel(logging.ERROR)


"""
Things still to do:
 - Support status of day/night and switching

IR switch URL:
http://X.X.X.X/ISAPI/Image/channels/1/ircutFilter
report IR status and allow

"""

# pylint: disable=too-many-instance-attributes
class HikCamera(object):
    """Creates a new Hikvision api device."""

    def __init__(self, host=None, port=DEFAULT_PORT,
                 usr=None, pwd=None, verify_ssl=True):
        """Initialize device."""

        _LOGGING.debug("pyHik %s initializing new hikvision device at: %s",
                       __version__, host)

        self.event_states = {}

        self.watchdog = Watchdog(300.0, self.watchdog_handler)

        if not host:
            _LOGGING.error('Host not specified! Cannot continue.')
            return

        self.host = host
        self.usr = usr
        self.pwd = pwd
        self.cam_id = 0
        self.name = ''
        self.device_type = None
        self.motion_detection = None
        self._motion_detection_xml = None

        self.root_url = '{}:{}'.format(host, port)

        self.namespace = {
            CONTEXT_INFO: None,
            CONTEXT_TRIG: None,
            CONTEXT_ALERT: None,
            CONTEXT_MOTION: None
        }

        # Build requests session for main thread calls
        # Default to basic authentication. It will change to digest inside
        # get_device_info if basic fails
        self.hik_request = requests.Session()

        self.hik_request.verify = verify_ssl

        self.hik_request.auth = (usr, pwd)
        self.hik_request.headers.update(DEFAULT_HEADERS)

        # Define event stream processing thread
        self.kill_thrd = threading.Event()
        self.reset_thrd = threading.Event()
        self.thrd = threading.Thread(
            target=self.alert_stream, args=(self.reset_thrd, self.kill_thrd,))
        self.thrd.daemon = False

        # Callbacks
        self._updateCallbacks = []

        self.initialize()

    @property
    def get_id(self):
        """Returns unique camera/nvr identifier."""
        return self.cam_id

    @property
    def get_name(self):
        """Return camera/nvr name."""
        return self.name

    @property
    def get_type(self):
        """Return device type."""
        return self.device_type

    @property
    def current_event_states(self):
        """Return Event states dictionary"""
        return self.event_states

    @property
    def current_motion_detection_state(self):
        """Return current state of motion detection property"""
        return self.motion_detection

    def get_motion_detection(self):
        """Fetch current motion state from camera"""
        url = ('%s/ISAPI/System/Video/inputs/'
               'channels/1/motionDetection') % self.root_url

        try:
            response = self.hik_request.get(url, timeout=CONNECT_TIMEOUT)
        except (requests.exceptions.RequestException,
                requests.exceptions.ConnectionError) as err:
            _LOGGING.error('Unable to fetch MotionDetection, error: %s', err)
            self.motion_detection = None
            return self.motion_detection

        if response.status_code == requests.codes.unauthorized:
            _LOGGING.error('Authentication failed')
            self.motion_detection = None
            return self.motion_detection

        if response.status_code != requests.codes.ok:
            # If we didn't receive 200, abort
            _LOGGING.debug('Unable to fetch motion detection.')
            self.motion_detection = None
            return self.motion_detection

        try:
            tree = ET.fromstring(response.text)
            self.fetch_namespace(tree, CONTEXT_MOTION)
            enabled = tree.find(self.element_query('enabled', CONTEXT_MOTION))

            if enabled is not None:
                self._motion_detection_xml = tree
            self.motion_detection = {'true': True, 'false': False}[enabled.text]
            return self.motion_detection

        except AttributeError as err:
            _LOGGING.error('Entire response: %s', response.text)
            _LOGGING.error('There was a problem: %s', err)
            self.motion_detection = None
            return self.motion_detection

    def enable_motion_detection(self):
        """Enable motion detection"""
        self._set_motion_detection(True)

    def disable_motion_detection(self):
        """Disable motion detection"""
        self._set_motion_detection(False)

    def _set_motion_detection(self, enable):
        """Set desired motion detection state on camera"""
        url = ('%s/ISAPI/System/Video/inputs/'
               'channels/1/motionDetection') % self.root_url

        enabled = self._motion_detection_xml.find(self.element_query('enabled', CONTEXT_MOTION))
        if enabled is None:
            _LOGGING.error("Couldn't find 'enabled' in the xml")
            _LOGGING.error('XML: %s', ET.tostring(self._motion_detection_xml))
            return

        enabled.text = 'true' if enable else 'false'
        xml = ET.tostring(self._motion_detection_xml)

        try:
            response = self.hik_request.put(url, data=xml, timeout=CONNECT_TIMEOUT)
        except (requests.exceptions.RequestException,
                requests.exceptions.ConnectionError) as err:
            _LOGGING.error('Unable to set MotionDetection, error: %s', err)
            return

        if response.status_code == requests.codes.unauthorized:
            _LOGGING.error('Authentication failed')
            return

        if response.status_code != requests.codes.ok:
            # If we didn't receive 200, abort
            _LOGGING.error('Unable to set motion detection: %s', response.text)

        self.motion_detection = enable

    def get_snapshot(self, channel=1):
        """
        Fetch a snapshot image from the camera.

        Args:
            channel: The channel number (1-based). For NVR devices, this is
                     the camera channel. For standalone cameras, use 1.

        Returns:
            bytes: The snapshot image data, or None if the request fails.
        """
        # Calculate stream channel based on device type
        # NVR uses channel * 100 + 1 format (e.g., channel 1 -> 101)
        # Standalone cameras use channel 1
        if self.device_type == NVR_DEVICE:
            stream_channel = channel * 100 + 1
        else:
            stream_channel = 1

        url = '%s/ISAPI/Streaming/channels/%d/picture' % (
            self.root_url, stream_channel)

        try:
            response = self.hik_request.get(url, timeout=SNAPSHOT_TIMEOUT)
        except requests.exceptions.Timeout:
            _LOGGING.warning('Timeout fetching snapshot from %s', self.name)
            return None
        except (requests.exceptions.RequestException,
                requests.exceptions.ConnectionError) as err:
            _LOGGING.error('Unable to fetch snapshot, error: %s', err)
            return None

        if response.status_code == requests.codes.unauthorized:
            _LOGGING.error('Authentication failed fetching snapshot')
            return None

        if response.status_code != requests.codes.ok:
            _LOGGING.debug('Unable to fetch snapshot: %s', response.status_code)
            return None

        return response.content

    def get_stream_url(self, channel=1, protocol='rtsp', stream_type=1):
        """
        Get the streaming URL for a camera channel.

        Args:
            channel: The channel number (1-based). For NVR devices, this is
                     the camera channel. For standalone cameras, use 1.
            protocol: The streaming protocol ('rtsp' is currently supported).
            stream_type: Stream type (1 for main stream, 2 for sub stream).

        Returns:
            str: The stream URL with encoded credentials, or None if protocol
                 is not supported.
        """
        if protocol != 'rtsp':
            _LOGGING.warning('Unsupported stream protocol: %s', protocol)
            return None

        # Calculate stream channel based on device type
        # NVR uses channel * 100 + stream_type format
        # Standalone cameras use stream_type directly
        if self.device_type == NVR_DEVICE:
            stream_channel = channel * 100 + stream_type
        else:
            stream_channel = stream_type

        # Extract host without port for RTSP URL
        host = self.host

        # URL encode credentials for safety
        encoded_user = quote(self.usr, safe='')
        encoded_pwd = quote(self.pwd, safe='')

        return 'rtsp://%s:%s@%s:%d/Streaming/Channels/%d' % (
            encoded_user, encoded_pwd, host, DEFAULT_RTSP_PORT, stream_channel)

    def get_channels(self):
        """
        Get the list of available channels.

        For NVR devices, returns a list of channel numbers based on the
        event triggers. For standalone cameras, returns [1].

        Returns:
            list: List of available channel numbers (1-based).
        """
        channels = set()

        if self.event_states:
            for event_type, event_list in self.event_states.items():
                for event_data in event_list:
                    # event_data format: [state, channel, count, timestamp]
                    channel = event_data[1]
                    if channel > 0:
                        channels.add(channel)

        if not channels:
            # Default to channel 1 if no events found
            channels.add(1)

        return sorted(list(channels))

    def add_update_callback(self, callback, sensor):
        """Register as callback for when a matching device sensor changes."""
        self._updateCallbacks.append([callback, sensor])
        _LOGGING.debug('Added update callback to %s on %s', callback, sensor)

    def _do_update_callback(self, msg):
        """Call registered callback functions."""
        for callback, sensor in self._updateCallbacks:
            if sensor == msg:
                _LOGGING.debug('Update callback %s for sensor %s',
                               callback, sensor)
                callback(msg)

    def element_query(self, element, context):
        """Build tree query for a given element and context."""
        if context == CONTEXT_INFO:
            return '{%s}%s' % (self.namespace[CONTEXT_INFO], element)
        elif context == CONTEXT_TRIG:
            return '{%s}%s' % (self.namespace[CONTEXT_TRIG], element)
        elif context == CONTEXT_ALERT:
            return '{%s}%s' % (self.namespace[CONTEXT_ALERT], element)
        elif context == CONTEXT_MOTION:
            return '{%s}%s' % (self.namespace[CONTEXT_MOTION], element)
        else:
            return '{%s}%s' % (XML_NAMESPACE, element)

    def fetch_namespace(self, tree, context):
        """Determine proper namespace to find given element."""
        if context == CONTEXT_INFO:
            nmsp = tree.tag.split('}')[0].strip('{')
            self.namespace[CONTEXT_INFO] = nmsp if nmsp.startswith('http') else XML_NAMESPACE
            _LOGGING.debug('Device info namespace: %s', self.namespace[CONTEXT_INFO])
        elif context == CONTEXT_TRIG:
            try:
                # For triggers we *typically* only care about the sub-namespace
                nmsp = tree[0][1].tag.split('}')[0].strip('{')
            except IndexError:
                # If get a index error check on top level
                nmsp = tree.tag.split('}')[0].strip('{')
            self.namespace[CONTEXT_TRIG] = nmsp if nmsp.startswith('http') else XML_NAMESPACE
            _LOGGING.debug('Device triggers namespace: %s', self.namespace[CONTEXT_TRIG])
        elif context == CONTEXT_ALERT:
            nmsp = tree.tag.split('}')[0].strip('{')
            self.namespace[CONTEXT_ALERT] = nmsp if nmsp.startswith('http') else XML_NAMESPACE
            _LOGGING.debug('Device alerts namespace: %s', self.namespace[CONTEXT_ALERT])
        elif context == CONTEXT_MOTION:
            nmsp = tree.tag.split('}')[0].strip('{')
            self.namespace[CONTEXT_MOTION] = nmsp if nmsp.startswith('http') else XML_NAMESPACE
            _LOGGING.debug('Device motion namespace: %s', self.namespace[CONTEXT_MOTION])

    def initialize(self):
        """Initialize deviceInfo and available events."""
        device_info = self.get_device_info()

        if device_info is None:
            self.name = None
            self.cam_id = None
            self.event_states = None
            return

        for key in device_info:
            if key == 'deviceName':
                self.name = device_info[key]
            elif key == 'deviceID':
                if len(device_info[key]) > 10:
                    self.cam_id = device_info[key]
                else:
                    self.cam_id = uuid.uuid4()

        events_available = self.get_event_triggers()
        if events_available:
            for event, channel_list in events_available.items():
                for channel in channel_list:
                    try:
                        # Tracking videoloss events causes problems since they are used
                        # as the watchdog so ignore them if they are enabled in the triggers.
                        if event.lower() != 'videoloss':
                            self.event_states.setdefault(
                                SENSOR_MAP[event.lower()], []).append(
                                    [False, channel, 0, datetime.datetime.now()])
                    except KeyError:
                        # Sensor type doesn't have a known friendly name
                        # We can't reliably handle it at this time...
                        _LOGGING.warning(
                            'Sensor type "%s" is unsupported.', event)

            _LOGGING.debug('Initialized Dictionary: %s', self.event_states)
        else:
            _LOGGING.debug('No Events available in dictionary.')

        self.get_motion_detection()

    def get_device_info(self):
        """Parse deviceInfo into dictionary."""
        device_info = {}
        url = '%s/ISAPI/System/deviceInfo' % self.root_url
        using_digest = False

        try:
            response = self.hik_request.get(url, timeout=CONNECT_TIMEOUT)
            if response.status_code == requests.codes.unauthorized:
                _LOGGING.debug('Basic authentication failed. Using digest.')
                self.hik_request.auth = HTTPDigestAuth(self.usr, self.pwd)
                using_digest = True
                response = self.hik_request.get(url)

            if response.status_code == requests.codes.not_found:
                # Try alternate URL for deviceInfo
                _LOGGING.debug('Using alternate deviceInfo URL.')
                url = '%s/System/deviceInfo' % self.root_url
                response = self.hik_request.get(url)
                # Seems to be difference between camera and nvr, they can't seem to
                # agree if they should 404 or 401 first
                if not using_digest and response.status_code == requests.codes.unauthorized:
                    _LOGGING.debug('Basic authentication failed. Using digest.')
                    self.hik_request.auth = HTTPDigestAuth(self.usr, self.pwd)
                    using_digest = True
                    response = self.hik_request.get(url)

        except (requests.exceptions.RequestException,
                requests.exceptions.ConnectionError) as err:
            _LOGGING.error('Unable to fetch deviceInfo, error: %s', err)
            return None

        if response.status_code == requests.codes.unauthorized:
            _LOGGING.error('Authentication failed')
            return None

        if response.status_code != requests.codes.ok:
            # If we didn't receive 200, abort
            _LOGGING.debug('Unable to fetch device info.')
            return None

        try:
            tree = ET.fromstring(response.text)
            self.fetch_namespace(tree, CONTEXT_INFO)
 
            for item in tree:
                tag = item.tag.split('}')[1]
                device_info[tag] = item.text

            return device_info

        except AttributeError as err:
            _LOGGING.error('Entire response: %s', response.text)
            _LOGGING.error('There was a problem: %s', err)
            return None

    def get_event_triggers(self, notification_methods=None):
        """
        Returns dict of supported events.
        Key = Event Type
        List = Channels that have that event activated

        Args:
            notification_methods: Set of notification method strings to accept.
                Defaults to {'center', 'HTTP'}. For NVRs, you may want to include
                additional methods like 'record', 'email', 'beep'.
        """
        if notification_methods is None:
            notification_methods = {'center', 'HTTP'}
        # Normalize to lowercase for comparison
        notification_methods_lower = {m.lower() for m in notification_methods}

        events = {}
        nvrflag = False
        event_xml = []

        # different firmware versions support different endpoints.
        urls = (
            '%s/ISAPI/Event/triggers',    # ISAPI v2.0+
            '%s/Event/triggers',          # Old devices?
        )
        response = {}

        for url in urls:
            try:
                response = self.hik_request.get(url % self.root_url, timeout=CONNECT_TIMEOUT)
                if response.status_code != requests.codes.ok:
                    # Try next alternate URL for triggers
                    _LOGGING.debug('Trying alternate triggers URL.')
                    continue

            except (requests.exceptions.RequestException,
                    requests.exceptions.ConnectionError) as err:
                _LOGGING.error('Unable to fetch events, error: %s', err)
                return None
            break
        else:
            _LOGGING.error('Unable to fetch events. '
                           'Device firmware may be old/bad.')
            return None

        # pylint: disable=too-many-nested-blocks
        try:
            content = ET.fromstring(response.text)
            self.fetch_namespace(content, CONTEXT_TRIG)

            if content[0].find(self.element_query('EventTrigger', CONTEXT_TRIG)):
                event_xml = content[0].findall(
                    self.element_query('EventTrigger', CONTEXT_TRIG))
            elif content.find(self.element_query('EventTrigger', CONTEXT_TRIG)):
                # This is either an NVR or a rebadged camera
                event_xml = content.findall(
                    self.element_query('EventTrigger', CONTEXT_TRIG))

            for eventtrigger in event_xml:
                ettype = eventtrigger.find(self.element_query('eventType', CONTEXT_TRIG))
                # Catch empty xml defintions
                if ettype is None:
                    break
                etnotify = eventtrigger.find(
                    self.element_query('EventTriggerNotificationList', CONTEXT_TRIG))

                etchannel = None
                etchannel_num = 0

                for node_name in CHANNEL_NAMES:
                    etchannel = eventtrigger.find(
                        self.element_query(node_name, CONTEXT_TRIG))
                    if etchannel is not None:
                        try:
                            # Need to make sure this is actually a number
                            etchannel_num = int(etchannel.text)
                            if etchannel_num > 1:
                                # Must be an nvr
                                nvrflag = True
                            break
                        except (ValueError, TypeError):
                            # Field must not be an integer
                            pass

                if etnotify:
                    for notifytrigger in etnotify:
                        ntype = notifytrigger.find(
                            self.element_query('notificationMethod', CONTEXT_TRIG))
                        if ntype is not None and ntype.text and \
                                ntype.text.lower() in notification_methods_lower:
                            # Found an event with a valid notification method
                            # Catch events with bad IDs
                            if etchannel_num == 0 : etchannel_num = 1
                            events.setdefault(ettype.text, []) \
                                .append(etchannel_num)

        except (AttributeError, ET.ParseError) as err:
            _LOGGING.error(
                'There was a problem finding an element: %s', err)
            return None

        if nvrflag:
            self.device_type = NVR_DEVICE
        else:
            self.device_type = CAM_DEVICE
        _LOGGING.debug('Processed %s as %s Device.',
                       self.cam_id, self.device_type)

        _LOGGING.debug('Found events: %s', events)
        self.hik_request.close()

        return events

    def watchdog_handler(self):
        """Take care of threads if wachdog expires."""
        _LOGGING.debug('%s Watchdog expired. Resetting connection.', self.name)
        self.watchdog.stop()
        self.reset_thrd.set()

    def disconnect(self):
        """Disconnect from event stream."""
        _LOGGING.debug('Disconnecting from stream: %s', self.name)
        self.kill_thrd.set()
        self.thrd.join()
        _LOGGING.debug('Event stream thread for %s is stopped', self.name)
        self.kill_thrd.clear()

    def start_stream(self):
        """Start thread to process event stream."""
        # self.watchdog.start()
        self.thrd.start()

    def alert_stream(self, reset_event, kill_event):
        """Open event stream."""
        _LOGGING.debug('Stream Thread Started: %s, %s', self.name, self.cam_id)
        start_event = False
        parse_string = ""
        fail_count = 0

        url = '%s/ISAPI/Event/notification/alertStream' % self.root_url

        # pylint: disable=too-many-nested-blocks
        while True:

            try:
                stream = self.hik_request.get(url, stream=True,
                                              timeout=(CONNECT_TIMEOUT,
                                                       READ_TIMEOUT))
                if stream.status_code == requests.codes.not_found:
                    # Try alternate URL for stream
                    url = '%s/Event/notification/alertStream' % self.root_url
                    stream = self.hik_request.get(url, stream=True)

                if stream.status_code != requests.codes.ok:
                    raise ValueError('Connection unsucessful.')
                else:
                    _LOGGING.debug('%s Connection Successful.', self.name)
                    fail_count = 0
                    self.watchdog.start()

                for line in stream.iter_lines():
                    # _LOGGING.debug('Processing line from %s', self.name)
                    # filter out keep-alive new lines
                    if line:
                        str_line = line.decode("utf-8", "ignore")
                        # New events start with --boundry
                        if str_line.find('<EventNotificationAlert') != -1:
                            # Start of event message
                            start_event = True
                            parse_string = str_line
                        elif str_line.find('</EventNotificationAlert>') != -1:
                            # Message end found found
                            parse_string += str_line
                            start_event = False
                            if parse_string:
                                try:
                                    tree = ET.fromstring(parse_string)
                                    self.process_stream(tree)
                                    self.update_stale()
                                except ET.ParseError as err:
                                    _LOGGING.warning('XML parse error in stream.')
                                parse_string = ""
                        else:
                            if start_event:
                                parse_string += str_line

                    if kill_event.is_set():
                        # We were asked to stop the thread so lets do so.
                        break
                    elif reset_event.is_set():
                        # We need to reset the connection.
                        raise ValueError('Watchdog failed.')

                if kill_event.is_set():
                    # We were asked to stop the thread so lets do so.
                    _LOGGING.debug('Stopping event stream thread for %s',
                                   self.name)
                    self.watchdog.stop()
                    self.hik_request.close()
                    return
                elif reset_event.is_set():
                    # We need to reset the connection.
                    raise ValueError('Watchdog failed.')

            except (ValueError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as err:
                fail_count += 1
                reset_event.clear()
                _LOGGING.warning('%s Connection Failed (count=%d). Waiting %ss. Err: %s',
                                 self.name, fail_count, (fail_count * 5) + 5, err)
                parse_string = ""
                self.watchdog.stop()
                self.hik_request.close()
                time.sleep(5)
                self.update_stale()
                time.sleep(fail_count * 5)
                continue

    def process_stream(self, tree):
        """Process incoming event stream packets."""
        if not self.namespace[CONTEXT_ALERT]:
            self.fetch_namespace(tree, CONTEXT_ALERT)

        try:
            etype = SENSOR_MAP[tree.find(
                self.element_query('eventType', CONTEXT_ALERT)).text.lower()]
            
            # Since this pasing is different and not really usefull for now, just return without error.
            if len(etype) > 0 and etype == 'Ongoing Events':
                return
            
            estate = tree.find(
                self.element_query('eventState', CONTEXT_ALERT)).text

            for idtype in ID_TYPES:
                echid = tree.find(self.element_query(idtype, CONTEXT_ALERT))
                if echid is not None:
                    try:
                        # Need to make sure this is actually a number
                        echid = int(echid.text)
                        break
                    except (ValueError, TypeError) as err:
                        # Field must not be an integer or is blank
                        pass

            ecount = tree.find(
                self.element_query('activePostCount', CONTEXT_ALERT)).text
        except (AttributeError, KeyError, IndexError) as err:
            _LOGGING.error('Problem finding attribute: %s', err)
            return

        # Take care of keep-alive
        if len(etype) > 0 and etype == 'Video Loss':
            self.watchdog.pet()

        # Track state if it's in the event list.
        if len(etype) > 0:
            state = self.fetch_attributes(etype, echid)
            if state:
                # Determine if state has changed
                # If so, publish, otherwise do nothing
                estate = (estate == 'active')
                old_state = state[0]
                attr = [estate, echid, int(ecount),
                        datetime.datetime.now()]
                self.update_attributes(etype, echid, attr)

                if estate != old_state:
                    self.publish_changes(etype, echid)
                self.watchdog.pet()

    def update_stale(self):
        """Update stale active statuses"""
        # Some events don't post an inactive XML, only active.
        # If we don't get an active update for 5 seconds we can
        # assume the event is no longer active and update accordingly.
        for etype, echannels in self.event_states.items():
            for eprop in echannels:
                if eprop[3] is not None:
                    sec_elap = ((datetime.datetime.now()-eprop[3])
                                .total_seconds())
                    # print('Seconds since last update: {}'.format(sec_elap))
                    if sec_elap > 5 and eprop[0] is True:
                        _LOGGING.debug('Updating stale event %s on CH(%s)',
                                       etype, eprop[1])
                        attr = [False, eprop[1], eprop[2],
                                datetime.datetime.now()]
                        self.update_attributes(etype, eprop[1], attr)
                        self.publish_changes(etype, eprop[1])

    def publish_changes(self, etype, echid):
        """Post updates for specified event type."""
        _LOGGING.debug('%s Update: %s, %s',
                       self.name, etype, self.fetch_attributes(etype, echid))
        signal = 'ValueChanged.{}'.format(self.cam_id)
        sender = '{}.{}'.format(etype, echid)
        if dispatcher:
            dispatcher.send(signal=signal, sender=sender)

        self._do_update_callback('{}.{}.{}'.format(self.cam_id, etype, echid))

    def fetch_attributes(self, event, channel):
        """Returns attribute list for a given event/channel."""
        try:
            for sensor in self.event_states[event]:
                if sensor[1] == int(channel):
                    return sensor
        except (KeyError, TypeError):
            return None

    def update_attributes(self, event, channel, attr):
        """Update attribute list for current event/channel."""
        try:
            for i, sensor in enumerate(self.event_states[event]):
                if sensor[1] == int(channel):
                    self.event_states[event][i] = attr
        except (KeyError, TypeError):
            _LOGGING.debug('Error updating attributes for: (%s, %s)',
                           event, channel)

    def inject_events(self, events):
        """Inject discovered events into the camera's event_states.

        This allows the camera to track events that wouldn't normally be
        detected, such as those from NVRs with non-standard notification
        methods.

        Args:
            events: Dict mapping event type names to lists of channel numbers.
        """
        for event_name, channels in events.items():
            for channel in channels:
                # Only add if not already present
                if event_name not in self.event_states:
                    self.event_states[event_name] = []

                # Check if this channel is already tracked
                channel_exists = any(
                    sensor[1] == channel for sensor in self.event_states[event_name]
                )
                if not channel_exists:
                    # Add the event state: [is_active, channel, count, last_update_time]
                    self.event_states[event_name].append(
                        [False, channel, 0, datetime.datetime.now()]
                    )

    def get_recording_days(self, track_id, start_date, end_date):
        """Get days with recordings available.

        Args:
            track_id: The track ID to search (e.g., 101 for channel 1).
            start_date: Start of the search range (datetime).
            end_date: End of the search range (datetime).

        Returns:
            List of RecordingDay objects sorted by date descending.
        """
        days_with_recordings = {}
        url = '%s/ISAPI/ContentMgmt/search' % self.root_url

        # Search in 1-day windows to ensure we get all dates
        window_size = datetime.timedelta(days=1)
        current_start = start_date

        while current_start < end_date:
            current_end = min(current_start + window_size, end_date)

            try:
                # Generate a unique searchID for each request
                search_id = str(uuid.uuid4()).upper()
                search_xml = '''<?xml version="1.0" encoding="utf-8"?>
<CMSearchDescription>
<searchID>{search_id}</searchID>
<trackIDList>
<trackID>{track_id}</trackID>
</trackIDList>
<timeSpanList>
<timeSpan>
<startTime>{start_time}Z</startTime>
<endTime>{end_time}Z</endTime>
</timeSpan>
</timeSpanList>
<maxResults>500</maxResults>
<searchResultPosition>0</searchResultPosition>
<metadataList>
<metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>
</metadataList>
</CMSearchDescription>'''.format(
                    search_id=search_id,
                    track_id=track_id,
                    start_time=current_start.strftime("%Y-%m-%dT%H:%M:%S"),
                    end_time=current_end.strftime("%Y-%m-%dT%H:%M:%S")
                )

                response = self.hik_request.post(
                    url,
                    data=search_xml,
                    headers={'Content-Type': 'application/xml'},
                    timeout=RECORDING_SEARCH_TIMEOUT
                )

                if response.status_code != requests.codes.ok:
                    current_start = current_end
                    continue

                root = ET.fromstring(response.text)

                # Find all searchMatchItem elements (handle namespace)
                for match in root.iter():
                    if 'searchMatchItem' in match.tag:
                        time_span = None
                        for child in match:
                            if 'timeSpan' in child.tag:
                                time_span = child
                                break
                        if time_span is None:
                            continue

                        start_time_elem = None
                        for child in time_span:
                            if 'startTime' in child.tag:
                                start_time_elem = child
                                break
                        if start_time_elem is None or not start_time_elem.text:
                            continue

                        # Handle Z suffix and parse datetime
                        time_str = start_time_elem.text.replace('Z', '+00:00')
                        try:
                            rec_date = datetime.datetime.fromisoformat(time_str)
                        except ValueError:
                            # Try without timezone
                            time_str = start_time_elem.text.rstrip('Z')
                            try:
                                rec_date = datetime.datetime.fromisoformat(time_str)
                            except ValueError:
                                continue

                        date_key = rec_date.strftime("%Y-%m-%d")
                        if date_key not in days_with_recordings:
                            days_with_recordings[date_key] = RecordingDay(
                                date=rec_date.replace(
                                    hour=0, minute=0, second=0, microsecond=0
                                ),
                                has_recordings=True
                            )

            except (requests.exceptions.RequestException,
                    requests.exceptions.ConnectionError,
                    ET.ParseError) as err:
                _LOGGING.warning('Failed to search recording days: %s', err)

            current_start = current_end

        return sorted(
            days_with_recordings.values(),
            key=lambda x: x.date,
            reverse=True
        )

    def search_recordings(self, track_id, start_time, end_time, max_results=100):
        """Search for recordings in a time range.

        Args:
            track_id: The track ID to search (e.g., 101 for channel 1).
            start_time: Start of the search range (datetime).
            end_time: End of the search range (datetime).
            max_results: Maximum number of results to return.

        Returns:
            List of Recording objects sorted by start_time descending.
        """
        # Generate a unique searchID for each request
        search_id = str(uuid.uuid4()).upper()
        search_xml = '''<?xml version="1.0" encoding="utf-8"?>
<CMSearchDescription>
<searchID>{search_id}</searchID>
<trackIDList>
<trackID>{track_id}</trackID>
</trackIDList>
<timeSpanList>
<timeSpan>
<startTime>{start_time}Z</startTime>
<endTime>{end_time}Z</endTime>
</timeSpan>
</timeSpanList>
<maxResults>{max_results}</maxResults>
<searchResultPosition>0</searchResultPosition>
<metadataList>
<metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>
</metadataList>
</CMSearchDescription>'''.format(
            search_id=search_id,
            track_id=track_id,
            start_time=start_time.strftime("%Y-%m-%dT%H:%M:%S"),
            end_time=end_time.strftime("%Y-%m-%dT%H:%M:%S"),
            max_results=max_results
        )

        recordings = []

        try:
            response = self.hik_request.post(
                '%s/ISAPI/ContentMgmt/search' % self.root_url,
                data=search_xml,
                headers={'Content-Type': 'application/xml'},
                timeout=RECORDING_SEARCH_TIMEOUT
            )

            if response.status_code == requests.codes.ok:
                root = ET.fromstring(response.text)
                recordings = self._parse_recording_results(root)

        except (requests.exceptions.RequestException,
                requests.exceptions.ConnectionError,
                ET.ParseError) as err:
            _LOGGING.warning('Failed to search recordings: %s', err)

        return recordings

    def _parse_recording_results(self, root):
        """Parse search results from XML response.

        Args:
            root: The root element of the XML response.

        Returns:
            List of Recording objects.
        """
        recordings = []

        for match in root.iter():
            if 'searchMatchItem' not in match.tag:
                continue

            try:
                source_id = ''
                track_id_val = 101
                rec_start = None
                rec_end = None
                playback_uri = ''
                content_type = 'video'

                for child in match:
                    tag_name = child.tag.split('}')[-1] if '}' in child.tag else child.tag

                    if tag_name == 'sourceID' and child.text:
                        source_id = child.text
                    elif tag_name == 'trackID' and child.text:
                        try:
                            track_id_val = int(child.text)
                        except ValueError:
                            pass
                    elif tag_name == 'timeSpan':
                        for time_child in child:
                            time_tag = time_child.tag.split('}')[-1] if '}' in time_child.tag else time_child.tag
                            if time_tag == 'startTime' and time_child.text:
                                try:
                                    rec_start = datetime.datetime.fromisoformat(
                                        time_child.text.replace('Z', '+00:00')
                                    )
                                except ValueError:
                                    rec_start = datetime.datetime.fromisoformat(
                                        time_child.text.rstrip('Z')
                                    )
                            elif time_tag == 'endTime' and time_child.text:
                                try:
                                    rec_end = datetime.datetime.fromisoformat(
                                        time_child.text.replace('Z', '+00:00')
                                    )
                                except ValueError:
                                    rec_end = datetime.datetime.fromisoformat(
                                        time_child.text.rstrip('Z')
                                    )
                    elif tag_name == 'mediaSegmentDescriptor':
                        for media_child in child:
                            media_tag = media_child.tag.split('}')[-1] if '}' in media_child.tag else media_child.tag
                            if media_tag == 'playbackURI' and media_child.text:
                                playback_uri = media_child.text
                            elif media_tag == 'contentType' and media_child.text:
                                content_type = media_child.text

                if rec_start is not None and rec_end is not None:
                    recordings.append(Recording(
                        source_id=source_id,
                        track_id=track_id_val,
                        start_time=rec_start,
                        end_time=rec_end,
                        content_type=content_type,
                        playback_uri=playback_uri
                    ))

            except (ValueError, AttributeError):
                continue

        return sorted(recordings, key=lambda x: x.start_time, reverse=True)


def inject_events_into_camera(camera, events):
    """Inject discovered events into the pyhik camera's event_states.

    This allows the camera to track events that wouldn't normally be detected.

    Args:
        camera: A HikCamera instance.
        events: Dict mapping event type names to lists of channel numbers.
    """
    camera.inject_events(events)


def get_video_channels(host, port, username, password, ssl=False):
    """Fetch available video input channels from Hikvision device.

    This queries the ISAPI to discover available camera channels on
    NVRs and standalone cameras.

    Args:
        host: Device hostname or IP address.
        port: HTTP port number.
        username: Authentication username.
        password: Authentication password.
        ssl: Whether to use HTTPS (default False).

    Returns:
        List of VideoChannel objects.
    """
    protocol = "https" if ssl else "http"
    root_url = "{}://{}:{}".format(protocol, host, port)
    channels = []

    session = requests.Session()
    session.auth = HTTPDigestAuth(username, password)
    session.verify = ssl

    # Try different ISAPI endpoints for channel discovery
    urls = [
        '{}/ISAPI/System/Video/inputs/channels'.format(root_url),
        '{}/ISAPI/ContentMgmt/InputProxy/channels'.format(root_url),
    ]

    response = None
    for url in urls:
        try:
            response = session.get(url, timeout=CONNECT_TIMEOUT)
            if response.status_code == requests.codes.ok:
                break
        except requests.exceptions.RequestException:
            continue

    if response is None or response.status_code != requests.codes.ok:
        # Fall back to streaming channels endpoint
        try:
            response = session.get(
                '{}/ISAPI/Streaming/channels'.format(root_url),
                timeout=CONNECT_TIMEOUT
            )
        except requests.exceptions.RequestException:
            session.close()
            return channels

    if response is None or response.status_code != requests.codes.ok:
        _LOGGING.warning('Unable to fetch video channels from device')
        session.close()
        return channels

    try:
        tree = ET.fromstring(response.text)
    except ET.ParseError as err:
        _LOGGING.error('Failed to parse video channels XML: %s', err)
        session.close()
        return channels

    # Handle namespace
    namespace = ""
    root_tag = tree.tag
    if root_tag.startswith("{"):
        namespace = root_tag.split("}")[0] + "}"

    # Try to find VideoInputChannel elements
    channel_elements = tree.findall(".//{}VideoInputChannel".format(namespace))

    # If not found, try InputProxyChannel
    if not channel_elements:
        channel_elements = tree.findall(".//{}InputProxyChannel".format(namespace))

    # If still not found, try StreamingChannel
    if not channel_elements:
        channel_elements = tree.findall(".//{}StreamingChannel".format(namespace))
        # Streaming channels have different structure - extract unique channel IDs
        seen_channels = set()
        for elem in channel_elements:
            channel_id_elem = elem.find("{}id".format(namespace))
            if channel_id_elem is not None and channel_id_elem.text:
                try:
                    # Channel IDs are formatted as (channel * 100) + stream_type
                    full_id = int(channel_id_elem.text)
                    channel_num = full_id // 100
                    if channel_num > 0 and channel_num not in seen_channels:
                        seen_channels.add(channel_num)
                        name_elem = elem.find("{}channelName".format(namespace))
                        channel_name = (
                            name_elem.text
                            if name_elem is not None and name_elem.text
                            else "Channel {}".format(channel_num)
                        )
                        enabled_elem = elem.find("{}enabled".format(namespace))
                        enabled = (
                            enabled_elem.text.lower() == "true"
                            if enabled_elem is not None and enabled_elem.text
                            else True
                        )
                        channels.append(VideoChannel(
                            id=channel_num,
                            name=channel_name,
                            enabled=enabled
                        ))
                except ValueError:
                    continue
        session.close()
        return channels

    # Process VideoInputChannel or InputProxyChannel elements
    for elem in channel_elements:
        channel_id_elem = elem.find("{}id".format(namespace))
        if channel_id_elem is None or not channel_id_elem.text:
            continue

        try:
            channel_id = int(channel_id_elem.text)
        except ValueError:
            continue

        # Get channel name
        name_elem = elem.find("{}name".format(namespace))
        if name_elem is None or not name_elem.text:
            name_elem = elem.find("{}channelName".format(namespace))
        channel_name = (
            name_elem.text
            if name_elem is not None and name_elem.text
            else "Channel {}".format(channel_id)
        )

        # Check if channel is enabled
        enabled = True
        enabled_elem = elem.find("{}enabled".format(namespace))
        if enabled_elem is not None and enabled_elem.text:
            enabled = enabled_elem.text.lower() == "true"

        # For InputProxyChannel, also check online status
        online_elem = elem.find("{}online".format(namespace))
        if online_elem is not None and online_elem.text:
            enabled = enabled and online_elem.text.lower() == "true"

        channels.append(VideoChannel(
            id=channel_id,
            name=channel_name,
            enabled=enabled
        ))

    session.close()
    return channels
