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
    'VMD-1': 'Motion',
    'IO': 'IO Trigger',
    'Linedetection-1': 'Line Crossing',
    'fielddetection-1': 'Field Detection',
    'videoloss-1': 'Video Loss',
    'tamper-1': 'Tamper Detection',
    'shelteralarm': 'Shelter Alarm',
    'diskfull': 'Disk Full',
    'diskerror': 'Disk Error',
    'nicbroken': 'Net Interface Broken',
    'ipconflict': 'IP Conflict',
    'illaccess': 'Illegal Access',
    'videomismatch': 'Video Mismatch',
    'badvideo': 'Bad Video',
    'PIR': 'PIR Alarm',
    'callhelp': 'Help Call',
    'facedetection': 'Face Detection',
    'WLSensor-1': 'WL Alarm 1',
    'WLSensor-2': 'WL Alarm 2',
    'WLSensor-3': 'WL Alarm 3',
    'WLSensor-4': 'WL Alarm 4',
    'WLSensor-5': 'WL Alarm 5',
    'WLSensor-6': 'WL Alarm 6',
    'WLSensor-7': 'WL Alarm 7',
    'WLSensor-8': 'WL Alarm 8',
}
