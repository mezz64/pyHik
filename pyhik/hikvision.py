"""
pyhik.hikvision
~~~~~~~~~~~~~~~~~~~~
Provides api for Hikvision events
Copyright (c) 2016 John Mihalic <https://github.com/mezz64>
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

try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

import threading
from pydispatch import dispatcher
import requests

from pyhik.constants import (
    DEFAULT_PORT, DEFAULT_HEADERS, XML_NAMESPACE, SENSOR_MAP)

_LOGGING = logging.getLogger(__name__)

# Hide nuisance requests logging
logging.getLogger('requests').setLevel(logging.CRITICAL)


"""
Things still to do:
 - Support status of motion detection and turning on/off
 - Support status of day/night and switching

Motion detection URL:
http://X.X.X.X/ISAPI/System/Video/inputs/channels/1/motionDetection

IR switch URL:
http://X.X.X.X/ISAPI/Image/channels/1/ircutFilter
report IR status and allow
"""


# pylint: disable=too-many-instance-attributes
class HikCamera(object):
    """Creates a new Hikvision api device."""

    def __init__(self, host=None, port=DEFAULT_PORT,
                 usr=None, pwd=None):
        """Initialize device."""

        _LOGGING.debug("Initializing new hikvision device at: %s", host)

        self.event_states = {}

        self.namespace = XML_NAMESPACE

        if not host:
            _LOGGING.error('Host not specified! Cannot continue.')
            return

        self.host = host
        self.usr = usr
        self.pwd = pwd
        self.cam_id = 0  # uuid.uuid4().hex
        self.name = ''

        self.root_url = '{}:{}'.format(host, port)

        self.etype = ''
        self.estate = False
        self.echid = 0
        self.ecount = 0

        # Build requests session for main thread calls
        self.hik_request = requests.Session()
        self.hik_request.auth = (usr, pwd)
        self.hik_request.timeout = 5
        self.hik_request.headers.update(DEFAULT_HEADERS)

        # Define event stream processing thread
        self.kill_thrd = threading.Event()
        self.thrd = threading.Thread(
            target=self.alert_stream, args=(self.kill_thrd,))
        self.thrd.daemon = False

        self.initialize()

    @property
    def get_id(self):
        """Returns unique camera identifier."""
        return self.cam_id

    @property
    def get_name(self):
        """Return camera name."""
        return self.name

    @property
    def current_event_states(self):
        """Return Event states dictionary"""
        return self.event_states

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
                self.cam_id = device_info[key]

        events_available = self.get_event_triggers()
        if events_available:
            for event in events_available:
                try:
                    self.event_states[SENSOR_MAP[event]] = [
                        False, 1, 0, datetime.datetime.now()]
                except KeyError:
                    # Sensor type doesn't have a known friendly name
                    # We can't reliably handle it at this time...
                    _LOGGING.warning(
                        'Sensor type "%s" is currently unsupported.', event)

            _LOGGING.debug('Initialized Dictionary: %s', self.event_states)
        else:
            _LOGGING.debug('No Events available in dictionary.')

    def get_event_triggers(self):
        """Returns list of supported events."""
        events = []

        url = '%s/ISAPI/Event/triggers' % self.root_url

        try:
            response = self.hik_request.get(url)
        except requests.exceptions.RequestException as err:
            _LOGGING.error('Unable to fetch events, error: %s', err)
            return None

        # Response of 200 means OK

        try:
            content = ET.fromstring(response.text)

            for eventtrigger in content[0].findall(
                    self.element_query('EventTrigger')):
                ettype = eventtrigger.find(self.element_query('eventType'))
                etnotify = eventtrigger.find(
                    self.element_query('EventTriggerNotificationList'))

                for notifytrigger in etnotify:
                    ntype = notifytrigger.find(
                        self.element_query('notificationMethod'))
                    if ntype.text == 'center':
                        """
                        If we got this far we found an event that we want to
                        track.
                        """
                        events.append(ettype.text)

        except (AttributeError, ET.ParseError) as err:
            _LOGGING.error(
                'There was a problem finding an element: %s', err)
            return None

        _LOGGING.debug('Found events: %s', events)
        self.hik_request.close()
        return events

    def get_device_info(self):
        """Parse deviceInfo into dictionary"""
        device_info = {}
        url = '%s/ISAPI/System/deviceInfo' % self.root_url

        try:
            response = self.hik_request.get(url)
        except requests.exceptions.RequestException as err:
            _LOGGING.error('Unable to fetch deviceInfo, error: %s', err)
            return None

        # Response of 200 means OK

        try:
            tree = ET.fromstring(response.text)
            # Try to fetch namespace from XML
            nmsp = tree.tag.split('}')[0].strip('{')
            self.namespace = nmsp if nmsp.startswith('http') else XML_NAMESPACE
            _LOGGING.debug('Using Namespace: %s', self.namespace)

            for item in tree:
                tag = item.tag.split('}')[1]
                device_info[tag] = item.text

            # print(device_info)
            return device_info

        except AttributeError as err:
            _LOGGING.error('Entire response: %s', response.text)
            _LOGGING.error('There was a problem: %s', err)
            return None

    def disconnect(self):
        """Disconnect from event stream."""
        _LOGGING.debug('Disconnecting from stream: %s', self.name)
        self.kill_thrd.set()
        self.thrd.join()
        _LOGGING.debug('Event stream thread for %s is stopped', self.name)
        self.kill_thrd.clear()

    def start_stream(self):
        """Start thread to process event stream."""
        self.thrd.start()

    def alert_stream(self, kill_event):
        """Open event stream."""
        _LOGGING.debug('Stream Thread Started: %s, %s', self.name, self.cam_id)
        start_event = False
        parse_string = ""

        # Need to see if we have any events available
        # before we start the stream

        url = '%s/ISAPI/Event/notification/alertStream' % self.root_url

        # pylint: disable=too-many-nested-blocks
        while True:
            try:
                stream = self.hik_request.get(url, stream=True)
                for line in stream.iter_lines():
                    # _LOGGING.debug('Processing line from %s', self.name)
                    # filter out keep-alive new lines
                    if line:  # and not kill_event.wait(1):
                        str_line = line.decode("utf-8")
                        # New events start with --boundry
                        if str_line.find('Content-Length') != -1:
                            # Start of event message
                            start_event = True
                        elif str_line.find('--boundary') != -1 or \
                                str_line.find('--hikboundary') != -1:
                            # Message boundry found
                            start_event = False
                            if parse_string:
                                tree = ET.fromstring(parse_string)
                                self.process_stream(tree)
                                self.update_stale()
                                parse_string = ""
                        else:
                            if start_event:
                                parse_string += str_line

                    if kill_event.is_set():
                        # We were asked to stop the thread so lets do so.
                        _LOGGING.debug('Stopping event stream thread for %s',
                                       self.name)
                        self.hik_request.close()
                        return

            except (ValueError, requests.exceptions.ChunkedEncodingError):
                _LOGGING.info('%s Connection Lost. Waiting 5s.', self.name)
                time.sleep(5)
                self.update_stale()
                continue

    def process_stream(self, tree):
        """Process incoming event stream packets."""
        try:
            self.etype = SENSOR_MAP[tree.findall(
                self.element_query('eventType'))[0].text]
            self.estate = tree.findall(
                self.element_query('eventState'))[0].text
            self.echid = tree.findall(
                self.element_query('channelID'))[0].text
            self.ecount = tree.findall(
                self.element_query('activePostCount'))[0].text
        except (AttributeError, KeyError, IndexError) as err:
            _LOGGING.error('Problem finding attribute: %s', err)
            return
        # Track state if it's in the event list.
        if len(self.etype) > 0 and self.etype in self.event_states:
            # Determine if state has changed
            # If so, update, otherwise do nothing
            self.estate = (self.estate == 'active')
            old_state = self.event_states[self.etype][0]
            self.event_states[self.etype] = [
                self.estate, int(self.echid), int(self.ecount),
                datetime.datetime.now()]
            if self.estate != old_state:
                self.publish_changes()

    def update_stale(self):
        """Update stale active statuses"""
        # Some events don't post an inactive XML, only active.
        # If we don't get an active update for 5 seconds we can
        # assume the event is no longer active and update accordingly.
        for self.etype, eprop in self.event_states.items():
            if eprop[3] is not None:
                sec_elap = ((datetime.datetime.now()-eprop[3]).total_seconds())
                # print('Seconds since last update: {}'.format(sec_elap))
                if sec_elap > 5 and eprop[0] is True:
                    self.event_states[self.etype] = [
                        False, eprop[1], eprop[2], datetime.datetime.now()]
                    self.publish_changes()

    def publish_changes(self):
        """Post updates for specified alarm type."""
        _LOGGING.debug('%s Update: %s, %s',
                       self.name, self.etype, self.event_states[self.etype])
        signal = 'ValueChanged.{}'.format(self.cam_id)
        dispatcher.send(signal=signal, sender=self.etype)
