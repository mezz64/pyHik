"""
pyhik.constants
~~~~~~~~~~~~~~~~~~~~
Constants list
Copyright (c) 2016 John Mihalic <https://github.com/mezz64>
Licensed under the MIT license.
"""

DEFAULT_PORT = 80
XML_ENCODING = 'UTF-8'
XML_NAMESPACE = 'http://www.hikvision.com/ver20/XMLSchema'

DEFAULT_HEADERS = {
    'Content-Type': "application/xml; charset='UTF-8'",
    'Accept': "*/*"
}

SENSOR_MAP = {
    'VMD': 'Motion',
    'IO': 'IO Trigger',
    'linedetection': 'Line Crossing',
    'fielddetection': 'Field Detection',
    'videoloss': 'Video Loss',
    'tamperdetection': 'Tamper Detection',
    'shelteralarm': 'Shelter Alarm',
    'diskfull': 'Disk Full',
    'diskerror': 'Disk Error',
    'nicbroken': 'Net Interface Broken',
    'ipconflict': 'IP Conflict',
    'illaccess': 'Illegal Access',
    'videomismatch': 'Video Mismatch',
    'badvideo': 'Bad Video',
    'PIR': 'PIR Alarm',
    'facedetection': 'Face Detection',
}
