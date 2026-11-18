"""
pyhik.constants
~~~~~~~~~~~~~~~~~~~~
Constants list
Copyright (c) 2016-2026 John Mihalic <https://github.com/mezz64>
Licensed under the MIT license.
"""

MAJOR_VERSION = 0
MINOR_VERSION = 5
SUB_MINOR_VERSION = 0
__version__ = '{}.{}.{}'.format(
    MAJOR_VERSION, MINOR_VERSION, SUB_MINOR_VERSION)

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
# Refusals of the credentials by the event stream, without a connection between
# them, before it is reported as refused and slowed down
STREAM_AUTH_FAILURES = 2
# Seconds between its attempts then. A locked account is usually open again
# after about this long, and one attempt per interval does not keep it locked.
STREAM_AUTH_RETRY_INTERVAL = 1800
SNAPSHOT_TIMEOUT = 10
RECORDING_SEARCH_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 300

DEFAULT_PORT = 80
DEFAULT_RTSP_PORT = 554
XML_ENCODING = 'UTF-8'
XML_NAMESPACE = 'http://www.hikvision.com/ver20/XMLSchema'

DEFAULT_HEADERS = {
    'Content-Type': "application/xml; charset='UTF-8'",
    'Accept': "*/*"
}

SENSOR_MAP = {
    'vmd': 'Motion',
    'linedetection': 'Line Crossing',
    'fielddetection': 'Field Detection',
    'videoloss': 'Video Loss',
    'tamperdetection': 'Tamper Detection',
    'shelteralarm': 'Tamper Detection',
    'defocus': 'Tamper Detection',
    'diskfull': 'Disk Full',
    'diskerror': 'Disk Error',
    'nicbroken': 'Net Interface Broken',
    'ipconflict': 'IP Conflict',
    'illaccess': 'Illegal Access',
    'videomismatch': 'Video Mismatch',
    'badvideo': 'Bad Video',
    'pir': 'PIR Alarm',
    'facedetection': 'Face Detection',
    'scenechangedetection': 'Scene Change Detection',
    'io': 'I/O',
    'unattendedbaggage': 'Unattended Baggage',
    'attendedbaggage': 'Attended Baggage',
    'recordingfailure': 'Recording Failure',
    'regionexiting': "Exiting Region",
    'regionentrance': "Entering Region",
    'duration': "Ongoing Events",
    'thermometry': "Thermometry"
}

# triggersCap lists <Name>TriggerCap elements; map the ones whose name
# differs from the event type used in /Event/triggers.
TRIGGERS_CAP_SUFFIX = 'TriggerCap'
TRIGGERS_CAP_EVENTS = {
    'motiondetection': 'VMD',
    'illacces': 'illaccess',
}

# The name 'id' should always be last
CHANNEL_NAMES = ['dynVideoInputChannelID', 'videoInputChannelID',
                 'dynInputIOPortID', 'inputIOPortID',
                 'id']

ID_TYPES = ['channelID', 'dynChannelID', 'inputIOPortID',
            'dynInputIOPortID']

CAM_DEVICE = 'CAM'
NVR_DEVICE = 'NVR'

CONTEXT_INFO = 'INFO'
CONTEXT_TRIG = 'TRIGGERS'
CONTEXT_ALERT = 'ALERTS'
CONTEXT_MOTION = 'MOTION'

# Notification methods that indicate the event is active/configured
# Expanded from original list of just 'center' and 'HTTP' to support NVRs
VALID_NOTIFICATION_METHODS = {'center', 'HTTP', 'record', 'email', 'beep'}
