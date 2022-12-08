"""
pyhik.constants
~~~~~~~~~~~~~~~~~~~~
Constants list
Copyright (c) 2016-2021 John Mihalic <https://github.com/mezz64>
Licensed under the MIT license.
"""

MAJOR_VERSION = 0
MINOR_VERSION = 3
SUB_MINOR_VERSION = 1
__version__ = '{}.{}.{}'.format(
    MAJOR_VERSION, MINOR_VERSION, SUB_MINOR_VERSION)

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60

DEFAULT_PORT = 80
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
    'duration': "Ongoing Events"
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
