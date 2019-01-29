#!/usr/bin/env python3

import logging
import requests
import unittest

from unittest.mock import MagicMock, patch, PropertyMock
from pyhik.hikvision import HikCamera

XML = """<MotionDetection xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
    <enabled>{}</enabled>
    <enableHighlight>true</enableHighlight>
    <samplingInterval>2</samplingInterval>
    <startTriggerTime>500</startTriggerTime>
    <endTriggerTime>500</endTriggerTime>
    <regionType>grid</regionType>
    <Grid>
        <rowGranularity>18</rowGranularity>
        <columnGranularity>22</columnGranularity>
    </Grid>
    <MotionDetectionLayout version="2.0">
        <sensitivityLevel>20</sensitivityLevel>
        <layout>
            <gridMap>000000000000000000000000000000000c007e0c007ffffc</gridMap>
        </layout>
    </MotionDetectionLayout>
</MotionDetection>"""


@patch("pyhik.hikvision.requests.Session")
class HikvisionTestCase(unittest.TestCase):
    @staticmethod
    def set_motion_detection_state(get, value):
        get.reset_mock()
        mock = get.return_value
        mock.reset_mock()
        type(mock).ok = PropertyMock(return_value=True)
        type(mock).status_code = PropertyMock(return_value=requests.codes.ok)
        type(mock).text = PropertyMock(
            return_value=XML.format("true" if value else "false")
        )
        return get

    @patch("pyhik.hikvision.HikCamera.get_device_info")
    @patch("pyhik.hikvision.HikCamera.get_event_triggers")
    def test_motion_detection(self, *args):
        session = args[-1].return_value
        get = session.get
        url = "localhost:80/ISAPI/System/Video/inputs/channels/1/motionDetection"

        # Motion detection disabled
        self.set_motion_detection_state(get, False)
        device = HikCamera(host="localhost")
        get.assert_called_once_with(url)
        self.assertIsNotNone(device)
        self.assertFalse(device.current_motion_detection_state)

        # Motion detection enabled
        self.set_motion_detection_state(get, True)
        device = HikCamera(host="localhost")
        self.assertIsNotNone(device)
        self.assertTrue(device.current_motion_detection_state)

        # Enable calls put with the expected data
        self.set_motion_detection_state(get, True)
        session.put.return_value = MagicMock(status_code=requests.codes.ok, ok=True)
        device.enable_motion_detection()
        session.put.assert_called_once_with(url, data=XML.format("true").encode())

        # Disable
        def change_get_response(url, data):
            self.set_motion_detection_state(get, False)
            return MagicMock(ok=True, status_code=requests.codes.ok)

        self.set_motion_detection_state(get, True)
        session.put = MagicMock(side_effect=change_get_response)
        device = HikCamera(host="localhost")
        self.assertTrue(device.current_motion_detection_state)
        device.disable_motion_detection()
        self.assertFalse(device.current_motion_detection_state)


if __name__ == "__main__":
    unittest.main()
