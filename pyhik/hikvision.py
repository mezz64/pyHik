"""
pyhik.hikvision
~~~~~~~~~~~~~~~~~~~~
Provides api for Hikvision events
Copyright (c) 2016-2019 John Mihalic <https://github.com/mezz64>
Licensed under the MIT license.

Based on the following api documentation:
System:
http://oversea-download.hikvision.com/uploadfile/Leaflet/ISAPI/HIKVISION%20ISAPI_2.0-IPMD%20Service.pdf
Imaging:
http://oversea-download.hikvision.com/uploadfile/Leaflet/ISAPI/HIKVISION%20ISAPI_2.0-Image%20Service.pdf
"""
import time
import datetime
import logging
import uuid

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
    DEFAULT_PORT, DEFAULT_HEADERS, XML_NAMESPACE, SENSOR_MAP,
    CAM_DEVICE, NVR_DEVICE, CONNECT_TIMEOUT, READ_TIMEOUT,
    __version__)


_LOGGING = logging.getLogger(__name__)


# Hide nuisance requests logging
logging.getLogger('urllib3').setLevel(logging.ERROR)


"""
Things still to do:
 - Support status of day/night and switching

IR switch URL:
http://X.X.X.X/ISAPI/Image/channels/1/ircutFilter
report IR status and allow

"""

# The name 'id' should always be last
CHANNEL_NAMES = ['dynVideoInputChannelID', 'videoInputChannelID',
                 'dynInputIOPortID', 'inputIOPortID',
                 'id']


# pylint: disable=too-many-instance-attributes
class HikCamera(object):
    """Creates a new Hikvision api device."""

    def __init__(self, host=None, port=DEFAULT_PORT,
                 usr=None, pwd=None):
        """Initialize device."""

        _LOGGING.debug("pyHik %s initializing new hikvision device at: %s",
                       __version__, host)

        self.event_states = {}

        self.watchdog = Watchdog(300.0, self.watchdog_handler)

        self.namespace = XML_NAMESPACE

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

        # Build requests session for main thread calls
        # Default to basic authentication. It will change to digest inside
        # get_device_info if basic fails
        self.hik_request = requests.Session()
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
            ET.register_namespace("", self.namespace)
            enabled = tree.find(self.element_query('enabled'))

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

        enabled = self._motion_detection_xml.find(self.element_query('enabled'))
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

    def element_query(self, element):
        """Build tree query for a given element."""
        return '{%s}%s' % (self.namespace, element)

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

    def get_event_triggers(self):
        """
        Returns dict of supported events.
        Key = Event Type
        List = Channels that have that event activated
        """
        events = {}
        nvrflag = False
        event_xml = []

        url = '%s/ISAPI/Event/triggers' % self.root_url

        try:
            response = self.hik_request.get(url, timeout=CONNECT_TIMEOUT)
            if response.status_code == requests.codes.not_found:
                # Try alternate URL for triggers
                _LOGGING.debug('Using alternate triggers URL.')
                url = '%s/Event/triggers' % self.root_url
                response = self.hik_request.get(url)

        except (requests.exceptions.RequestException,
                requests.exceptions.ConnectionError) as err:
            _LOGGING.error('Unable to fetch events, error: %s', err)
            return None

        if response.status_code != 200:
            # If we didn't recieve 200, abort
            return None

        # pylint: disable=too-many-nested-blocks
        try:
            content = ET.fromstring(response.text)

            if content[0].find(self.element_query('EventTrigger')):
                event_xml = content[0].findall(
                    self.element_query('EventTrigger'))
            elif content.find(self.element_query('EventTrigger')):
                # This is either an NVR or a rebadged camera
                event_xml = content.findall(
                    self.element_query('EventTrigger'))

            for eventtrigger in event_xml:
                ettype = eventtrigger.find(self.element_query('eventType'))
                # Catch empty xml defintions
                if ettype is None:
                    break
                etnotify = eventtrigger.find(
                    self.element_query('EventTriggerNotificationList'))

                etchannel = None
                etchannel_num = 0

                for node_name in CHANNEL_NAMES:
                    etchannel = eventtrigger.find(
                        self.element_query(node_name))
                    if etchannel is not None:
                        try:
                            # Need to make sure this is actually a number
                            etchannel_num = int(etchannel.text)
                            if etchannel_num > 1:
                                # Must be an nvr
                                nvrflag = True
                            break
                        except ValueError:
                            # Field must not be an integer
                            pass

                if etnotify:
                    for notifytrigger in etnotify:
                        ntype = notifytrigger.find(
                            self.element_query('notificationMethod'))
                        if ntype.text == 'center' or ntype.text == 'HTTP':
                            """
                            If we got this far we found an event that we want
                            to track.
                            """
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
            # Try to fetch namespace from XML
            nmsp = tree.tag.split('}')[0].strip('{')
            self.namespace = nmsp if nmsp.startswith('http') else XML_NAMESPACE
            _LOGGING.debug('Using Namespace: %s', self.namespace)

            for item in tree:
                tag = item.tag.split('}')[1]
                device_info[tag] = item.text

            return device_info

        except AttributeError as err:
            _LOGGING.error('Entire response: %s', response.text)
            _LOGGING.error('There was a problem: %s', err)
            return None

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
                            parse_string += str_line
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
        try:
            etype = SENSOR_MAP[tree.find(
                self.element_query('eventType')).text.lower()]
            estate = tree.find(
                self.element_query('eventState')).text
            echid = tree.find(
                self.element_query('channelID'))
            if echid is None:
                # Some devices use a different key
                echid = tree.find(
                    self.element_query('dynChannelID'))
            echid = int(echid.text)
            ecount = tree.find(
                self.element_query('activePostCount')).text
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
        except KeyError:
            return None

    def update_attributes(self, event, channel, attr):
        """Update attribute list for current event/channel."""
        try:
            for i, sensor in enumerate(self.event_states[event]):
                if sensor[1] == int(channel):
                    self.event_states[event][i] = attr
        except KeyError:
            _LOGGING.debug('Error updating attributes for: (%s, %s)',
                           event, channel)
