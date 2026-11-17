#!/usr/bin/env python3
"""Tests for pyhik.checkisapi, the live check run by hand against a real device."""

import logging
from pathlib import Path
import re
import requests
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from pyhik import __main__ as pyhik_main
from pyhik import checkisapi
from pyhik.image import ImageSetting, ImageSettingType as T
from pyhik.isapi import ISAPIAuthError, ISAPIClient, ISAPIConnectionError, ISAPIError
from test.test_image_write import CAPS, FakeCamera, VALUES as DOCUMENT, get_text

CHANNELS = (Path(__file__).parent / "fixtures" / "image_channels_list_fixed_basic-DS-2CD2385G1.xml").read_bytes()
DEVICE_INFO = (
    b'<DeviceInfo version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
    b"<model>DS-2CD2385G1-I</model><deviceType>IPCamera</deviceType>"
    b"<firmwareVersion>V5.6.5</firmwareVersion></DeviceInfo>"
)
BLC_ON = "<enabled>true</enabled>\n<BLCMode>AUTO</BLCMode>\n</BLC>"
BLC_OFF = "<enabled>false</enabled>\n</BLC>"

E503 = ISAPIError("Request failed with status 503", 503, "serviceUnavailable")
E400 = ISAPIError("bad", 400, "badXmlContent")
E401 = ISAPIAuthError("Invalid credentials", 401)
E403 = ISAPIAuthError("Insufficient permissions", 403)
NO_CONNECTION = ISAPIConnectionError("Cannot connect to 192.168.1.100")


class FakeDetect:
    """Answers the two calls detect_proxy makes."""

    def __init__(self, device_type, camera, proxy):
        self.device_type, self.camera, self.proxy = device_type, camera, proxy
        self.calls = []

    def get_device_type(self):
        if isinstance(self.device_type, Exception):
            raise self.device_type
        return self.device_type

    def get_image_channels(self, proxy=False):
        self.calls.append(proxy)
        answer = self.proxy if proxy else self.camera
        if isinstance(answer, Exception):
            raise answer
        return answer


class TestDetectProxy(unittest.TestCase):
    def detect(self, client, flag=None):
        return checkisapi.detect_proxy(client, flag, lambda line: None)

    def test_decisions(self):
        cases = (
            ("type unreadable, camera 503, proxy lists", FakeDetect(E503, E503, [1]), None, True),
            ("type unreadable, camera 400, proxy lists", FakeDetect(E503, E400, [1]), None, True),
            ("type unreadable, camera works", FakeDetect(E503, [1], E503), None, False),
            ("type unreadable (no connection), camera works", FakeDetect(NO_CONNECTION, [1], E503), None, False),
            ("type unreadable, both fail", FakeDetect(E503, E503, E503), None, False),
            ("type IPC, camera 503, proxy lists", FakeDetect("IPC", E503, [1]), None, True),
            ("type IPC, camera works", FakeDetect("IPC", [1], [1]), None, False),
            ("type NVR", FakeDetect("NVR", E503, [1]), None, True),
            ("type DVR", FakeDetect("dvr", E503, [1]), None, True),
            ("--no-proxy wins", FakeDetect("NVR", E503, [1]), False, False),
            ("--proxy wins", FakeDetect("IPC", [1], E503), True, True),
        )
        for name, client, flag, expected in cases:
            with self.subTest(name):
                self.assertIs(self.detect(client, flag), expected)

    def test_an_nvr_type_needs_no_probe(self):
        client = FakeDetect("NVR", E503, [1])
        self.detect(client)
        self.assertEqual(client.calls, [])

    def test_an_authentication_or_connection_error_is_not_tried_on_another_endpoint(self):
        cases = (
            ("device type 401", FakeDetect(E401, [1], [1]), E401, []),
            ("device type 403", FakeDetect(E403, [1], [1]), E403, []),
            ("camera 401", FakeDetect("IPC", E401, [1]), E401, [False]),
            ("camera 401, type unreadable", FakeDetect(E503, E401, [1]), E401, [False]),
            ("camera 403", FakeDetect("IPC", E403, [1]), E403, [False]),
            ("camera connection error", FakeDetect("IPC", NO_CONNECTION, [1]), NO_CONNECTION, [False]),
            ("proxy 401", FakeDetect("IPC", E503, E401), E401, [False, True]),
            ("proxy connection error", FakeDetect("IPC", E503, NO_CONNECTION), NO_CONNECTION, [False, True]),
        )
        for name, client, error, calls in cases:
            with self.subTest(name):
                with self.assertRaises(type(error)) as raised:
                    self.detect(client)
                self.assertIs(raised.exception, error)
                self.assertEqual(client.calls, calls)


class TestDeviceLine(unittest.TestCase):
    def line(self, device_type):
        client = FakeDetect(device_type, [1], [1])
        client.get_device_model = client.get_firmware_version = client.get_device_type
        lines = []
        checkisapi._device_line(client, lines.append)
        return lines

    def test_a_device_that_does_not_answer_is_only_noted(self):
        for error in (E503, NO_CONNECTION):
            with self.subTest(error):
                self.assertIn("model=? (%s" % type(error).__name__, self.line(error)[-1])

    def test_an_authentication_error_is_raised(self):
        for error in (E401, E403):
            with self.subTest(error), self.assertRaises(ISAPIAuthError):
                self.line(error)


class Reply:
    """An HTTP response, as far as ISAPIClient looks at one."""

    def __init__(self, status_code=200, content=b""):
        self.status_code, self.content, self.headers = status_code, content, {}
        self.text = content.decode()


class RejectsEveryLogin:
    """Stands in for the client's HTTP session: a device that answers 401 to everything."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return Reply(401, b"<html><body>Unauthorized</body></html>")

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class TestWrongPassword(unittest.TestCase):
    def test_the_run_stops_at_the_first_authentication_error(self):
        client = ISAPIClient("192.168.1.100", username="admin", password="wrong")
        client._session = session = RejectsEveryLogin()
        lines = []
        with self.assertRaises(ISAPIAuthError):
            checkisapi.run(checkisapi.remember_device_info(client), out=lines.append)
        # The first request is all that is sent: the client tries Digest for it, then Basic.
        self.assertEqual([url.rsplit("/", 1)[1] for _, url in session.calls], ["deviceInfo", "deviceInfo"])
        self.assertTrue(lines[0].startswith("pyhik "), "the version is printed before anything is asked")


class Result:
    error_description = "refused by the fake"

    def __init__(self, success=True, also_changed=None):
        self.success = success
        self.also_changed = also_changed or {}

    def __str__(self):
        return "ok" if self.success else "failed"


class FakeDevice:
    """Keeps values; turning WDR on switches HLC off, as a device that has the conflict."""

    def __init__(self, values, fail_restore=False):
        self.values = dict(values)
        self.sets = []
        self.fail_restore = fail_restore

    def get_image_values(self, channel=None, proxy=False):
        return dict(self.values)

    def set_image_setting(self, path, value, channel=None, proxy=False):
        self.sets.append((path, value))
        if self.fail_restore and len(self.sets) > 1 and path == "Color/brightnessLevel":
            return Result(False)
        also = {}
        if path == "WDR/enabled" and value == "true" and self.values.get("HLC/enabled") == "true":
            self.values["HLC/enabled"] = "false"
            also = {"HLC/enabled": "false"}
        self.values[path] = value
        return Result(True, also)

    def set_image_linked_setting(self, setting, value, channel=None, proxy=False):
        return self.set_image_setting(setting.path, value, channel, proxy)


class DriftingDevice(FakeDevice):
    """Something else (the camera itself, another user) changes a setting during the write."""

    def set_image_setting(self, path, value, channel=None, proxy=False):
        result = super().set_image_setting(path, value, channel, proxy)
        if path == "Color/brightnessLevel":
            self.values["Color/contrastLevel"] = "7"
        return result


class ErrorDevice(FakeDevice):
    """Raises an error on the numbered call to set_image_setting or get_image_values."""

    def __init__(self, values, write_errors=None, read_errors=None):
        super().__init__(values)
        self.write_errors = write_errors or {}
        self.read_errors = read_errors or {}
        self.reads = 0

    def get_image_values(self, channel=None, proxy=False):
        self.reads += 1
        if self.reads in self.read_errors:
            raise self.read_errors[self.reads]
        return super().get_image_values(channel, proxy)

    def set_image_setting(self, path, value, channel=None, proxy=False):
        error = self.write_errors.get(len(self.sets) + 1)
        if error:
            self.sets.append((path, value))
            raise error
        return super().set_image_setting(path, value, channel, proxy)


SETTINGS = [
    ImageSetting("Color/brightnessLevel", T.NUMBER, min_value=0, max_value=100, value="50"),
    ImageSetting("Color/contrastLevel", T.NUMBER, min_value=0, max_value=100, value="100"),
    ImageSetting("WDR/enabled", T.SWITCH, value="false"),
    ImageSetting("HLC/enabled", T.SWITCH, value="true"),
    ImageSetting("IrcutFilter/IrcutFilterType", T.SELECT, options=["auto", "day", "night"], value="auto"),
    ImageSetting("OnlyOne/mode", T.SELECT, options=["a"], value="a"),
    ImageSetting("Unset/x", T.NUMBER, min_value=0, max_value=5, value=None),
]
VALUES = {s.path: s.value for s in SETTINGS if s.value is not None}


class TestCheckWrite(unittest.TestCase):
    def write(self, path, device=None):
        device = device or FakeDevice(VALUES)
        lines = []
        report = checkisapi.Report(lines.append)
        checkisapi.check_write(device, report, 1, False, SETTINGS, path, lines.append)
        return device, report, lines

    def test_default_is_a_number_moved_one_step_and_restored(self):
        device, report, lines = self.write(None)
        self.assertEqual(device.sets, [("Color/brightnessLevel", "51"), ("Color/brightnessLevel", "50")])
        self.assertEqual(device.values, VALUES)
        self.assertFalse(report.failed)
        self.assertEqual(lines[-2:], [
            "   to try a switch too: --write --path WDR/enabled | HLC/enabled",
            "   to try a select too: --write --path IrcutFilter/IrcutFilterType",  # not the one with one option
        ])

    def test_number_at_its_maximum_goes_down(self):
        device, _, _ = self.write("Color/contrastLevel")
        self.assertEqual(device.sets[0], ("Color/contrastLevel", "99"))
        self.assertEqual(device.values, VALUES)

    def test_select_takes_another_option(self):
        device, report, _ = self.write("IrcutFilter/IrcutFilterType")
        self.assertEqual(device.sets[0], ("IrcutFilter/IrcutFilterType", "day"))
        self.assertEqual(device.values, VALUES)
        self.assertFalse(report.failed)

    def test_switch_is_flipped(self):
        device, _, _ = self.write("HLC/enabled")
        self.assertEqual(device.sets[0], ("HLC/enabled", "false"))
        self.assertEqual(device.values, VALUES)

    def test_setting_switched_off_to_make_room_is_put_back(self):
        device, report, lines = self.write("WDR/enabled")
        self.assertEqual(device.values, VALUES)
        self.assertIn(("HLC/enabled", "true"), device.sets)
        self.assertTrue(any("also switched off" in line for line in lines))
        self.assertFalse(report.failed)

    def test_nothing_to_try_is_skipped(self):
        for path in ("OnlyOne/mode", "No/such", "Unset/x"):
            with self.subTest(path):
                device, report, _ = self.write(path)
                self.assertEqual(device.sets, [])
                self.assertEqual([status for _, status, _ in report.results], ["SKIP"])

    def test_the_default_is_the_first_number_that_can_move(self):
        stuck = ImageSetting("Color/brightnessLevel", T.NUMBER, min_value=50, max_value=50, value="50")
        absent = ImageSetting("Mode/OffLevel", T.NUMBER, min_value=0, max_value=100, value="19")
        device = FakeDevice(VALUES)
        report = checkisapi.Report(lambda line: None)
        checkisapi.check_write(device, report, 1, False, [stuck, absent] + SETTINGS[1:], None, lambda line: None)
        self.assertEqual(device.sets, [("Color/contrastLevel", "99"), ("Color/contrastLevel", "100")])

    def test_a_refused_write_says_why(self):
        class Refuses(FakeDevice):
            def set_image_setting(self, path, value, channel=None, proxy=False):
                self.sets.append((path, value))
                return Result(False)

        device, report, lines = self.write(None, Refuses(VALUES))
        self.assertEqual(report.results, [("write", "FAIL", "refused: refused by the fake")])
        self.assertEqual(device.sets, [("Color/brightnessLevel", "51")], "nothing changed, nothing to put back")

    def test_a_write_that_does_not_take_says_what_was_read_back(self):
        class Ignores(FakeDevice):
            def set_image_setting(self, path, value, channel=None, proxy=False):
                self.sets.append((path, value))
                return Result(True)

        _, report, _ = self.write(None, Ignores(VALUES))
        self.assertEqual(report.results, [("write", "FAIL", "accepted, but read back 50")])

    def test_failed_restore_is_reported(self):
        _, report, lines = self.write(None, FakeDevice(VALUES, fail_restore=True))
        self.assertTrue(report.failed)
        self.assertTrue(any("NOT RESTORED" in line for line in lines))

    def test_only_what_the_write_switched_off_is_put_back(self):
        device, report, lines = self.write(None, DriftingDevice(VALUES))
        self.assertEqual(device.sets, [("Color/brightnessLevel", "51"), ("Color/brightnessLevel", "50")])
        self.assertEqual(device.values["Color/contrastLevel"], "7", "not ours to change")
        self.assertFalse(report.failed)
        self.assertIn("   note: also different from before, and not changed by pyhik as far as this check "
                      "knows: Color/contrastLevel was 100, is 7", lines)

    def test_an_error_while_restoring_does_not_hide_the_first_error(self):
        device = ErrorDevice(VALUES, write_errors={2: ISAPIError("restore failed")},
                             read_errors={2: ISAPIError("read back failed")})
        lines = []
        report = checkisapi.Report(lines.append)
        with self.assertRaisesRegex(ISAPIError, "read back failed"):
            checkisapi.check_write(device, report, 1, False, SETTINGS, None, lines.append)
        self.assertTrue(report.failed)
        self.assertTrue(any("NOT RESTORED" in line and "restore failed" in line for line in lines))

    def test_no_request_is_sent_after_an_authentication_error(self):
        device = ErrorDevice(VALUES, write_errors={1: ISAPIAuthError("bad", 401)})
        lines = []
        report = checkisapi.Report(lines.append)
        with self.assertRaises(ISAPIAuthError):
            checkisapi.check_write(device, report, 1, False, SETTINGS, None, lines.append)
        self.assertEqual(device.sets, [("Color/brightnessLevel", "51")], "no restore attempt")
        self.assertTrue(report.failed)
        self.assertTrue(any("NOT TRIED" in line for line in lines))

    def test_an_authentication_error_while_restoring_stops_the_restore_and_the_run(self):
        device = ErrorDevice(VALUES, write_errors={2: ISAPIAuthError("bad", 401)})
        lines = []
        report = checkisapi.Report(lines.append)
        with self.assertRaises(ISAPIAuthError):
            checkisapi.check_write(device, report, 1, False, SETTINGS, "WDR/enabled", lines.append)
        self.assertEqual(device.sets, [("WDR/enabled", "true"), ("WDR/enabled", "false")],
                         "HLC/enabled is not tried after the authentication error")
        self.assertTrue(report.failed)
        self.assertTrue(any("NOT TRIED" in line and "HLC/enabled" in line for line in lines))


LOST = "connection lost"


class SimCamera(FakeCamera):
    """The FakeCamera of the write tests, with the other documents checkisapi reads.

    It stands in for the HTTP session of a real ISAPIClient, so pyhik's own conflict
    handling runs. With forgets_blc_mode the document has no BLCMode while BLC is off, as
    on a real camera, and BLC switched on without a mode comes on with the mode UP.

    fail={number: what} makes the request with that number (the first is 1) fail, and
    fail_from=(number, what) every request from that one on. what is an HTTP status, or
    LOST for a connection that breaks before the request reaches the device.
    """

    def __init__(self, xml=DOCUMENT, forgets_blc_mode=False, fail=None, fail_from=None, caps=CAPS, **kwargs):
        super().__init__(xml, **kwargs)
        self.caps = caps
        self.forgets_blc_mode = forgets_blc_mode
        self.fail = fail or {}
        self.fail_from = fail_from
        self.calls = []  # (method, path)

    def request(self, method, url, **kwargs):
        path = urlparse(url).path
        self.calls.append((method, path))
        what = self.fail.get(len(self.calls))
        if what is None and self.fail_from and len(self.calls) >= self.fail_from[0]:
            what = self.fail_from[1]
        if what is LOST:
            raise requests.exceptions.ConnectionError("connection lost")
        if what:
            return Reply(what, b"")
        if method == "GET" and path.endswith("/capabilities"):
            return Reply(200, self.caps)
        if method == "GET" and path == "/ISAPI/Image/channels":
            return Reply(200, CHANNELS)
        if method == "GET" and path == "/ISAPI/System/deviceInfo":
            return Reply(200, DEVICE_INFO)
        reply = super().request(method, url, **kwargs)
        if self.forgets_blc_mode and get_text(self.xml, "BLC/enabled") != "true":
            self.xml = re.sub(r"<BLCMode>[^<]*</BLCMode>\n", "", self.xml)
        elif self.forgets_blc_mode and get_text(self.xml, "BLC/BLCMode") is None:
            self.xml = self.xml.replace("<enabled>true</enabled>\n</BLC>", BLC_ON.replace("AUTO", "UP"))
        return reply

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class DeviceTest(unittest.TestCase):
    """Runs a check with a real ISAPIClient against a SimCamera."""

    def setUp(self):
        # pyhik logs what the device refuses; that is not test output.
        quiet = logging.NullHandler()
        logging.getLogger("pyhik").addHandler(quiet)
        self.addCleanup(logging.getLogger("pyhik").removeHandler, quiet)

    def connect(self, camera=None):
        self.camera = camera or SimCamera()
        self.client = ISAPIClient("192.168.1.100", username="admin", password="pw")
        self.client._auth = object()  # the way to log in is already known
        self.client._session = self.camera
        self.settings = self.client.get_image_settings(1)
        self.found = self.camera.xml
        self.lines = []
        self.report = checkisapi.Report(self.lines.append)
        self.camera.calls.clear()

    def write(self, path, camera=None, fail=None, fail_from=None):
        self.connect(camera)
        self.camera.fail, self.camera.fail_from = fail or {}, fail_from
        checkisapi.check_write(self.client, self.report, 1, False, self.settings, path, self.lines.append)

    def assert_left_as_found(self):
        self.assertEqual(self.camera.xml, self.found)
        self.assertFalse(self.report.failed, self.lines)

    def restores(self):
        return [line.strip() for line in self.lines if line.startswith("   restore")]


class TestWriteOnADevice(DeviceTest):
    def test_a_number(self):
        self.write(None)
        self.assertEqual([get_text(body, "Color/brightnessLevel") for _, body in self.camera.puts], ["81", "80"])
        self.assert_left_as_found()
        self.assertEqual(self.restores(), ["restore Color/brightnessLevel -> 80: restored"])

    def test_what_made_room_comes_back_with_its_mode(self):
        # WDR cannot be switched on while BLC and HLC are on, so pyhik switches both off.
        self.write("WDR/mode", SimCamera(forgets_blc_mode=True))
        self.assertIn("   also switched off to make room: {'BLC/enabled': 'false', 'HLC/enabled': 'false'}", self.lines)
        self.assert_left_as_found()
        self.assertEqual(self.restores(), [
            "restore WDR/mode -> close: restored",
            "restore HLC/enabled -> true: restored",
            "restore BLC/enabled -> true, BLC/BLCMode -> AUTO: restored",
        ])
        # Three writes for the change (refused, make room, accepted) and one for each restore:
        # WDR is switched off before BLC and HLC come back, so no restore is refused.
        self.assertEqual(len(self.camera.puts), 6)

    def test_a_linked_select_is_switched_off_and_back_on(self):
        self.write("BLC/BLCMode", SimCamera(forgets_blc_mode=True))
        self.assertIn("   BLC/BLCMode (select): AUTO -> CLOSE", self.lines)
        self.assertEqual(self.report.results[0], ("write", "PASS", "read back OK"))
        self.assert_left_as_found()

    def test_a_linked_select_is_switched_on_and_back_off(self):
        self.write("BLC/BLCMode", SimCamera(DOCUMENT.replace(BLC_ON, BLC_OFF), forgets_blc_mode=True))
        self.assertIn("   BLC/BLCMode (select): CLOSE -> UP", self.lines)
        self.assertEqual(self.report.results[0], ("write", "PASS", "read back OK"))
        self.assert_left_as_found()
        self.assertEqual(self.restores(), ["restore BLC/enabled -> false: restored"])

    def test_a_setting_the_device_does_not_report_right_now_is_skipped(self):
        # Listed with the default of the capabilities; the document has no AdvancedMode while
        # the noise reduction mode is "general".
        self.write("NoiseReduce/AdvancedMode/FrameNoiseReduceLevel")
        self.assertEqual(self.camera.puts, [])
        self.assertEqual(self.report.results, [(
            "write", "SKIP", "NoiseReduce/AdvancedMode/FrameNoiseReduceLevel is not in the device's current "
            "values: it belongs to a mode or feature that is off (take another path)")])

    def test_the_suggested_paths_can_be_changed(self):
        self.write(None)
        self.assertEqual(self.lines[-2:], [
            "   to try a switch too: --write --path ImageFlip/enabled | Exposure/OverexposeSuppress/enabled | HLC/enabled",
            "   to try a select too: --write --path WDR/mode | BLC/BLCMode | IrcutFilter/IrcutFilterType",
        ])

    def test_a_restore_the_device_accepts_but_does_not_apply_is_reported(self):
        class KeepsBrightness(SimCamera):
            def request(self, method, url, **kwargs):
                if method == "PUT" and self.puts:
                    kwargs["data"] = self.xml.encode()
                return super().request(method, url, **kwargs)

        self.write(None, KeepsBrightness())
        self.assertTrue(self.report.failed)
        self.assertEqual(self.restores(), [
            "restore Color/brightnessLevel -> 80: NOT RESTORED (the device now has "
            "Color/brightnessLevel=81), set it back by hand"])


class TestWriteThatRaises(DeviceTest):
    """Switching WDR on while BLC and HLC are on takes these requests after the first read:
    2 and 3 the refused write, 4 to 6 pyhik switching BLC and HLC off, 7 and 8 the write again.
    """

    def test_what_pyhik_switched_off_is_put_back_when_the_write_then_raises(self):
        for number in (7, 8):
            with self.subTest(request=number):
                with self.assertRaisesRegex(ISAPIConnectionError, "Cannot connect"):
                    self.write("WDR/mode", SimCamera(forgets_blc_mode=True), fail={number: LOST})
                self.assert_left_as_found()
                self.assertEqual(self.restores(), [
                    "restore HLC/enabled -> true: restored",
                    "restore BLC/enabled -> true, BLC/BLCMode -> AUTO: restored",
                ])

    def test_nothing_is_written_when_the_write_raised_before_it_changed_anything(self):
        with self.assertRaises(ISAPIConnectionError):
            self.write("WDR/mode", fail={2: LOST})
        self.assertEqual(self.camera.puts, [])
        self.assert_left_as_found()
        self.assertEqual(self.restores(), ["restore: nothing differs from before, nothing to put back"])

    def test_a_device_that_stays_unreachable_is_reported(self):
        with self.assertRaises(ISAPIConnectionError):
            self.write("WDR/mode", fail_from=(7, LOST))
        self.assertEqual(get_text(self.camera.xml, "HLC/enabled"), "false", "left switched off")
        self.assertTrue(self.report.failed)
        self.assertEqual(self.restores(), [
            "restore: could not read what the device has now (ISAPIConnectionError: Cannot connect to 192.168.1.100)",
            "restore: NOT CHECKED what else was changed before the error, compare WDR, HLC, BLC and defog "
            "with the values listed above",
            "restore WDR/mode -> close: NOT RESTORED (ISAPIConnectionError: Cannot connect to 192.168.1.100), "
            "set it back by hand",
        ])

    def test_an_authentication_error_sends_nothing_more(self):
        with self.assertRaises(ISAPIAuthError):
            self.write("WDR/mode", fail_from=(7, 401))
        # Request 7 was refused; the client then asked once whether Digest would be accepted.
        self.assertEqual(len(self.camera.calls), 8)
        self.assertTrue(self.report.failed)
        self.assertEqual(self.restores(), [
            "restore: NOT CHECKED what else was changed before the error, compare WDR, HLC, BLC and defog "
            "with the values listed above",
            "restore WDR/mode -> close: NOT TRIED after the authentication error, set it back by hand",
        ])


def document(wdr="close", blc=True, hlc=True):
    """The camera's document with WDR, BLC and HLC as given (as shipped: BLC and HLC are on)."""
    xml = DOCUMENT if blc else DOCUMENT.replace(BLC_ON, BLC_OFF)
    if not hlc:
        xml = xml.replace("<enabled>true</enabled>\n<HLCLevel>", "<enabled>false</enabled>\n<HLCLevel>")
    return xml.replace("<mode>close</mode>\n<WDRLevel>", "<mode>%s</mode>\n<WDRLevel>" % wdr)


class TestConflictsOnADevice(DeviceTest):
    """With BLC and HLC on to begin with, the check sends 123 requests to this camera: 1 to
    119 fill the table, then the restore reads (120), writes BLC back (121, 122) and checks (123).
    """

    def conflicts(self, camera=None, fail=None, fail_from=None, without=()):
        self.connect(camera or SimCamera(forgets_blc_mode=True))
        self.camera.fail, self.camera.fail_from = fail or {}, fail_from
        settings = [s for s in self.settings if s.path not in without]
        checkisapi.check_conflicts(self.client, self.report, 1, False, settings, self.lines.append)

    def test_every_pair_is_tried_and_the_device_is_left_as_found(self):
        self.conflicts()
        self.assert_left_as_found()
        self.assertEqual(self.report.results, [
            ("conflicts", "PASS", "8 cases: 2 fine, 6 resolved by switching the other off, 0 refused")])
        self.assertIn("   BLC on, then WDR=open: BLC was switched off (by pyhik: BLC/enabled) after MutexWithWDR",
                      self.lines)
        # The last case left only the BLC mode different from before.
        self.assertEqual(self.restores(), ["restore BLC/enabled -> true, BLC/BLCMode -> AUTO: restored"])

    def test_whatever_was_on_before_is_on_again(self):
        for wdr, blc, hlc in (("open", False, False), ("auto", False, False), ("close", False, False),
                              ("close", True, False), ("close", False, True)):
            with self.subTest(wdr=wdr, blc=blc, hlc=hlc):
                self.conflicts(SimCamera(document(wdr, blc, hlc), forgets_blc_mode=True))
                self.assert_left_as_found()

    def test_an_authentication_error_sends_nothing_more(self):
        for number in (2, 30, 77, 119):
            with self.subTest(request=number):
                with self.assertRaisesRegex(ISAPIAuthError, "Invalid credentials"):
                    self.conflicts(fail_from=(number, 401))
                # The refused request, then the client asking once whether Digest would be accepted.
                self.assertEqual(len(self.camera.calls), number + 1)
                self.assertTrue(self.report.failed)
                self.assertEqual(self.restores(), [
                    "restore: NOT CHECKED what else was changed before the error, compare WDR, HLC, BLC and "
                    "defog with the values listed above",
                    "restore WDR/mode -> close: NOT TRIED after the authentication error, set it back by hand",
                    "restore HLC/enabled -> true: NOT TRIED after the authentication error, set it back by hand",
                    "restore BLC/enabled -> true, BLC/BLCMode -> AUTO: NOT TRIED after the authentication error, "
                    "set it back by hand",
                ])

    def test_what_pyhik_switched_off_is_put_back_even_if_the_check_does_not_try_it(self):
        # HLC is on, but in a form this check does not turn into a row of the table.
        self.conflicts(without=("HLC/enabled",))
        self.assertIn("   before: WDR/mode=close, BLC/BLCMode=AUTO, BLC/enabled=true", self.lines)
        self.assert_left_as_found()
        self.assertIn("restore HLC/enabled -> true: restored", self.restores())

    def test_everything_that_differs_is_put_back_after_an_error(self):
        # Requests 9 and 10 are pyhik switching HLC off; the write it does that for is lost (11).
        with self.assertRaises(ISAPIConnectionError):
            self.conflicts(without=("HLC/enabled",), fail={11: LOST})
        self.assert_left_as_found()
        self.assertEqual(self.restores(), [
            "restore HLC/enabled -> true: restored",
            "restore BLC/enabled -> true, BLC/BLCMode -> AUTO: restored",
        ])

    def test_the_log_level_is_only_lowered_as_far_as_needed(self):
        class NotesTheLevel(SimCamera):
            levels = set()

            def request(self, method, url, **kwargs):
                self.levels.add(logging.getLogger("pyhik.isapi").getEffectiveLevel())
                return super().request(method, url, **kwargs)

        root, package = logging.getLogger(), logging.getLogger("pyhik")
        self.addCleanup(root.setLevel, root.level)
        for level, during in ((logging.WARNING, logging.INFO), (logging.DEBUG, logging.DEBUG)):  # without and with -v
            with self.subTest(level=level):
                root.setLevel(level)
                NotesTheLevel.levels = set()
                self.conflicts(NotesTheLevel())
                # The first request is sent before the check listens to the log.
                self.assertEqual(NotesTheLevel.levels, {level, during})
                self.assertEqual(package.level, logging.NOTSET)
                self.assertIn("   HLC on, then WDR=open: HLC was switched off (by pyhik: HLC/enabled) after MutexWithWDR",
                              self.lines)

    def test_a_case_that_cannot_start_from_everything_off_is_not_tried(self):
        class WdrStaysOn(SimCamera):
            def request(self, method, url, **kwargs):
                data = kwargs.get("data")
                if method == "PUT" and get_text(self.xml, "WDR/mode") != "close" \
                        and get_text(data.decode(), "WDR/mode") == "close":
                    self.calls.append((method, urlparse(url).path))
                    return Reply(500, b"")
                return super().request(method, url, **kwargs)

        self.conflicts(WdrStaysOn(document("open", False, False)))
        self.assertIn("   HLC on, then BLC=CENTER: could not switch WDR off first", self.lines)
        self.assertEqual(self.report.results, [(
            "conflicts", "SKIP", "8 cases: 0 fine, 0 resolved by switching the other off, 0 refused, "
            "8 could not be tried")])
        self.assertEqual(self.restores(), ["restore: nothing differs from before, nothing to put back"])

    def test_blc_that_is_a_plain_switch_is_in_the_table(self):
        # No off option in the mode list, so pyhik does not link the mode to the flag.
        self.conflicts(SimCamera(caps=CAPS.replace(b"CLOSE,UP,", b"UP,")))
        self.assertIn("   before: WDR/mode=close, HLC/enabled=true, BLC/enabled=true", self.lines)
        self.assertIn("   BLC on, then WDR=open: BLC was switched off (by pyhik: BLC/enabled) after MutexWithWDR",
                      self.lines)
        self.assertEqual(self.report.results[-1][2], "8 cases: 2 fine, 6 resolved by switching the other off, 0 refused")
        self.assert_left_as_found()

    def test_hlc_that_is_a_linked_select_is_in_the_table(self):
        caps = CAPS.replace(b"<HLCLevel min", b'<HLCMode opt="close,open">open</HLCMode>\n<HLCLevel min')
        xml = DOCUMENT.replace("<HLCLevel>", "<HLCMode>open</HLCMode>\n<HLCLevel>")
        self.conflicts(SimCamera(xml, caps=caps))
        self.assertIn("   before: WDR/mode=close, HLC/HLCMode=open, HLC/enabled=true, BLC/BLCMode=AUTO, "
                      "BLC/enabled=true", self.lines)
        self.assertIn("   HLC on, then WDR=open: HLC was switched off (by pyhik: HLC/enabled) after MutexWithWDR",
                      self.lines)
        self.assertEqual(self.report.results[-1][2], "8 cases: 2 fine, 6 resolved by switching the other off, 0 refused")
        self.assert_left_as_found()

    def test_an_error_does_not_stop_the_restore(self):
        for number in (2, 30, 77, 119):
            with self.subTest(request=number):
                with self.assertRaises(ISAPIConnectionError):
                    self.conflicts(fail={number: LOST})
                self.assert_left_as_found()

    # In the next three, read 31 fails in the middle of the table with HLC and BLC switched
    # off. The restore then reads (32), writes HLC (33, 34) and BLC (35, 36) back and checks (37).

    def test_a_setting_that_cannot_be_put_back_does_not_stop_the_others(self):
        with self.assertRaisesRegex(ISAPIError, "status 503"):
            self.conflicts(fail={31: 503, 33: LOST})
        self.assertEqual(self.restores(), [
            "restore HLC/enabled -> true: NOT RESTORED (ISAPIConnectionError: Cannot connect to 192.168.1.100), "
            "set it back by hand",
            "restore BLC/enabled -> true, BLC/BLCMode -> AUTO: restored",
        ])
        self.assertEqual(self.report.results, [
            ("restore of HLC/enabled", "FAIL", "check it and set it back by hand: HLC/enabled -> true")])

    def test_a_restore_the_device_refuses_is_reported(self):
        with self.assertRaisesRegex(ISAPIError, "status 503"):
            self.conflicts(fail={31: 503, 36: 500})
        self.assertEqual(self.restores(), [
            "restore HLC/enabled -> true: restored",
            "restore BLC/enabled -> true, BLC/BLCMode -> AUTO: NOT RESTORED (refused: invalidResponse), "
            "set it back by hand",
        ])

    def test_an_authentication_error_while_restoring_stops_the_restore(self):
        with self.assertRaisesRegex(ISAPIError, "status 503"):
            self.conflicts(fail={31: 503}, fail_from=(34, 401))
        self.assertEqual(len(self.camera.calls), 35)
        self.assertEqual(self.restores(), [
            "restore HLC/enabled -> true: NOT RESTORED (ISAPIAuthError: Invalid credentials, status=401), "
            "set it back by hand",
            "restore BLC/enabled -> true, BLC/BLCMode -> AUTO: NOT TRIED after the authentication error, "
            "set it back by hand",
        ])

    def test_an_authentication_error_at_the_end_of_the_restore_stops_the_run(self):
        with self.assertRaises(ISAPIAuthError):
            self.conflicts(fail_from=(123, 401))
        self.assertEqual(self.restores(), [
            "restore BLC/enabled -> true, BLC/BLCMode -> AUTO: NOT CHECKED (could not read it back: "
            "ISAPIAuthError: Invalid credentials, status=401), set it back by hand"])

    def test_a_device_that_stays_unreachable_is_reported(self):
        with self.assertRaises(ISAPIConnectionError):
            self.conflicts(fail_from=(30, LOST))
        self.assertTrue(self.report.failed)
        self.assertEqual(len([line for line in self.restores() if "NOT RESTORED" in line]), 3)

    def test_fewer_than_two_features_is_skipped(self):
        self.connect()
        settings = [s for s in self.settings if s.path in ("WDR/mode", "Color/brightnessLevel")]
        checkisapi.check_conflicts(self.client, self.report, 1, False, settings, self.lines.append)
        self.assertEqual(self.camera.calls, [])
        self.assertEqual(self.report.results[0][:2], ("conflicts", "SKIP"))


class TestWhatElseDiffers(DeviceTest):
    class ResetsTheWdrLevel(SimCamera):
        """Forgets its WDR level whenever WDR is switched."""

        def request(self, method, url, **kwargs):
            mode = get_text(self.xml, "WDR/mode")
            reply = super().request(method, url, **kwargs)
            if get_text(self.xml, "WDR/mode") != mode:
                self.xml = re.sub("<WDRLevel>[0-9]+</WDRLevel>", "<WDRLevel>50</WDRLevel>", self.xml)
            return reply

    NOTE = ("   note: also different from before, and not changed by pyhik as far as this check knows: "
            "WDR/WDRLevel was 41, is 50")

    def test_it_is_listed_after_a_write(self):
        self.write("WDR/mode", self.ResetsTheWdrLevel())
        self.assertIn(self.NOTE, self.lines)
        self.assertFalse(self.report.failed)

    def test_it_is_listed_after_the_conflicts(self):
        self.connect(self.ResetsTheWdrLevel())
        checkisapi.check_conflicts(self.client, self.report, 1, False, self.settings, self.lines.append)
        self.assertIn(self.NOTE, self.lines)

    def test_nothing_is_listed_when_nothing_else_differs(self):
        self.write("WDR/mode")
        self.assertFalse([line for line in self.lines if "note:" in line])


class TestRestoreSteps(unittest.TestCase):
    def steps(self, before, paths):
        client = unittest.mock.Mock()
        settings = [ImageSetting("BLC/BLCMode", T.SELECT, options=["CLOSE", "UP", "AUTO"], value="AUTO",
                                 linked_enabled_path="BLC/enabled", off_value="CLOSE")]
        steps = checkisapi.restore_steps(client, 1, False, settings, before, paths)
        for _, write in steps:
            write()
        return [wanted for wanted, _ in steps], client.mock_calls

    def test_what_was_off_comes_first_and_a_linked_pair_is_one_write(self):
        before = {"WDR/mode": "open", "HLC/enabled": "false", "BLC/enabled": "true", "BLC/BLCMode": "AUTO",
                  "Dehaze/DehazeMode": "close"}
        wanted, calls = self.steps(before, ["WDR/mode", "BLC/BLCMode", "HLC/enabled", "BLC/enabled", "Dehaze/DehazeMode"])
        self.assertEqual(wanted, [{"HLC/enabled": "false"}, {"Dehaze/DehazeMode": "close"},
                                  {"WDR/mode": "open"}, {"BLC/enabled": "true", "BLC/BLCMode": "AUTO"}])
        self.assertEqual([call[0] for call in calls], ["set_image_setting", "set_image_setting",
                                                       "set_image_setting", "set_image_linked_setting"])
        self.assertEqual(calls[-1].args[1], "AUTO")

    def test_a_linked_select_that_was_off_gets_its_exact_values_back(self):
        for before in ({"BLC/enabled": "false"}, {"BLC/enabled": "false", "BLC/BLCMode": "UP"},
                       {"BLC/enabled": "true", "BLC/BLCMode": "CLOSE"}, {"BLC/enabled": "true"}):
            with self.subTest(before):
                wanted, calls = self.steps(before, ["BLC/enabled"])
                self.assertEqual(wanted, [before])
                self.assertEqual(calls, [unittest.mock.call.put_image_settings(before, 1, proxy=False)])


class TestOtherValue(unittest.TestCase):
    def test_each_kind(self):
        by_path = {s.path: s for s in SETTINGS}
        self.assertEqual(checkisapi.other_value(by_path["Color/brightnessLevel"]), "51")
        self.assertEqual(checkisapi.other_value(by_path["Color/contrastLevel"]), "99")
        self.assertEqual(checkisapi.other_value(by_path["WDR/enabled"]), "true")
        self.assertEqual(checkisapi.other_value(by_path["HLC/enabled"]), "false")
        self.assertEqual(checkisapi.other_value(by_path["IrcutFilter/IrcutFilterType"]), "day")
        self.assertIsNone(checkisapi.other_value(by_path["OnlyOne/mode"]))
        self.assertIsNone(checkisapi.other_value(by_path["Unset/x"]))

    def test_a_number_stays_in_its_range(self):
        def number(value, low, high):
            return checkisapi.other_value(ImageSetting("x", T.NUMBER, min_value=low, max_value=high, value=value))

        self.assertEqual(number("0", 0.0, 1.0), "1")
        self.assertEqual(number("1", 0.0, 1.0), "0")
        self.assertIsNone(number("7", 7.0, 7.0))
        self.assertIsNone(number("120", 0.0, 100.0))
        self.assertIsNone(number("1.5", 0.0, 100.0))
        self.assertIsNone(number("7", None, None))


ENV = {"CAMERA_HOST": "env-host", "CAMERA_PORT": "8080", "CAMERA_USERNAME": "env-user", "CAMERA_PASSWORD": "pw"}


class TestSettings(unittest.TestCase):
    def test_environment_only(self):
        with patch.dict("os.environ", ENV, clear=True):
            cfg = checkisapi.settings_from_env()
        self.assertEqual((cfg["host"], cfg["port"], cfg["username"], cfg["password"]),
                         ("env-host", 8080, "env-user", "pw"))

    def test_defaults(self):
        with patch.dict("os.environ", {"CAMERA_HOST": "h", "CAMERA_PASSWORD": "pw"}, clear=True):
            cfg = checkisapi.settings_from_env()
        self.assertEqual((cfg["port"], cfg["username"], cfg["ssl"], cfg["verify_ssl"]), (80, "admin", False, True))

    def test_command_line_wins_over_the_environment(self):
        with patch.dict("os.environ", ENV, clear=True):
            cfg = checkisapi.settings_from_env(host="arg-host", port=81, username="arg-user")
        self.assertEqual((cfg["host"], cfg["port"], cfg["username"]), ("arg-host", 81, "arg-user"))

    def test_https_uses_port_443_unless_a_port_is_given(self):
        base = {"CAMERA_HOST": "h", "CAMERA_PASSWORD": "pw"}
        with patch.dict("os.environ", base, clear=True):
            self.assertEqual(checkisapi.settings_from_env(ssl=True)["port"], 443)
            self.assertEqual(checkisapi.settings_from_env(ssl=True, port=8443)["port"], 8443)
            self.assertEqual(checkisapi.settings_from_env()["port"], 80)
        with patch.dict("os.environ", dict(base, CAMERA_SSL="1"), clear=True):
            cfg = checkisapi.settings_from_env()
        self.assertEqual((cfg["ssl"], cfg["port"]), (True, 443))

    def test_ssl_and_certificate_options_win_over_the_environment(self):
        with patch.dict("os.environ", dict(ENV, CAMERA_SSL="0", CAMERA_VERIFY_SSL="1"), clear=True):
            cfg = checkisapi.settings_from_env(ssl=True, verify_ssl=False)
            self.assertEqual((cfg["ssl"], cfg["verify_ssl"]), (True, False))
            cfg = checkisapi.settings_from_env()
            self.assertEqual((cfg["ssl"], cfg["verify_ssl"]), (False, True))
        with patch.dict("os.environ", dict(ENV, CAMERA_VERIFY_SSL="0"), clear=True):
            self.assertFalse(checkisapi.settings_from_env()["verify_ssl"])

    def test_host_on_the_command_line_is_enough(self):
        with patch.dict("os.environ", {"CAMERA_PASSWORD": "pw"}, clear=True):
            self.assertEqual(checkisapi.settings_from_env(host="h")["host"], "h")

    def test_password_only_comes_from_the_environment(self):
        with patch.dict("os.environ", ENV, clear=True):
            self.assertEqual(checkisapi.settings_from_env(password="other")["password"], "other")
        with patch.dict("os.environ", {"CAMERA_HOST": "h"}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                checkisapi.settings_from_env()
        self.assertIn("CAMERA_PASSWORD", str(raised.exception))


class TestMain(unittest.TestCase):
    def test_asks_for_the_environment_variables(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                checkisapi.main([])
        self.assertIn("CAMERA_HOST", str(raised.exception))
        self.assertIn("--host", str(raised.exception))

    def test_host_port_and_username_reach_the_client_and_the_checks(self):
        with patch.object(checkisapi, "client_from_env") as client, \
                patch.object(checkisapi, "run", return_value=0) as run:
            self.assertEqual(checkisapi.main(["--host", "h", "--port", "81", "--username", "u", "--bad-password"]), 0)
        expected = {"host": "h", "port": 81, "username": "u", "ssl": None, "verify_ssl": None}
        client.assert_called_once_with(**expected)
        self.assertEqual(run.call_args.kwargs["overrides"], expected)

    def test_ssl_and_no_verify_reach_the_client_and_the_checks(self):
        with patch.object(checkisapi, "client_from_env") as client, \
                patch.object(checkisapi, "run", return_value=0) as run:
            checkisapi.main(["--ssl", "--no-verify"])
        expected = {"host": None, "port": None, "username": None, "ssl": True, "verify_ssl": False}
        client.assert_called_once_with(**expected)
        self.assertEqual(run.call_args.kwargs["overrides"], expected)

    def test_not_given_means_the_environment(self):
        with patch.object(checkisapi, "client_from_env") as client, \
                patch.object(checkisapi, "run", return_value=0):
            checkisapi.main([])
        client.assert_called_once_with(host=None, port=None, username=None, ssl=None, verify_ssl=None)

    def test_the_connection_is_closed_whatever_happens(self):
        for outcome, code in ((0, 0), (1, 1), (E401, 2), (E503, 2), (NO_CONNECTION, 2)):
            with self.subTest(outcome):
                with patch.object(checkisapi, "client_from_env") as client, patch("builtins.print"), \
                        patch.object(checkisapi, "run", side_effect=[outcome]):
                    self.assertEqual(checkisapi.main([]), code)
                client.return_value.close.assert_called_once_with()

    def test_path_without_write_is_refused(self):
        with patch.object(checkisapi, "client_from_env") as client, patch("sys.stderr"), \
                self.assertRaises(SystemExit) as raised:
            checkisapi.main(["--path", "WDR/mode"])
        self.assertEqual(raised.exception.code, 2)
        client.assert_not_called()

    def test_path_goes_with_write_or_all(self):
        for flag in ("--write", "--all"):
            with self.subTest(flag):
                with patch.object(checkisapi, "client_from_env"), \
                        patch.object(checkisapi, "run", return_value=0) as run:
                    checkisapi.main([flag, "--path", "WDR/mode"])
                self.assertEqual(run.call_args.args[3:5], (True, "WDR/mode"))


class TestBadPassword(unittest.TestCase):
    def check(self, client_error, camera_id, auth_failed):
        client = MagicMock()
        client.get_image_channels.side_effect = client_error
        camera = MagicMock(get_id=camera_id, auth_failed=auth_failed)
        report = checkisapi.Report(lambda line: None)
        with patch.dict("os.environ", ENV, clear=True), \
                patch.object(checkisapi, "ISAPIClient", return_value=client) as new_client, \
                patch("pyhik.hikvision.HikCamera", return_value=camera) as new_camera:
            checkisapi.check_bad_password(report, lambda line: None, {"host": "arg-host"})
        self.assertEqual(new_client.call_args.kwargs["password"], "pw-wrong")
        self.assertEqual(new_camera.call_args.args, ("http://arg-host", 8080, "env-user", "pw-wrong", True))
        client.close.assert_called_once_with()
        return [(name.split("(")[1].rstrip(")"), status) for name, status, _ in report.results]

    def test_both_clients_report_it(self):
        self.assertEqual(self.check(E401, None, True),
                         [("ISAPIClient", "PASS"), ("HikCamera.auth_failed", "PASS")])

    def test_a_device_that_accepts_the_wrong_password_fails(self):
        self.assertEqual(self.check(None, "DS-2CD2385G1-I20190101AAWRD12345678", False),
                         [("ISAPIClient", "FAIL"), ("HikCamera.auth_failed", "FAIL")])

    def test_another_error_is_not_an_authentication_failure(self):
        self.assertEqual(self.check(NO_CONNECTION, None, False),
                         [("ISAPIClient", "FAIL"), ("HikCamera.auth_failed", "FAIL")])


class TestPackageCommand(unittest.TestCase):
    """python -m pyhik <command>"""

    def run_main(self, argv):
        with patch("builtins.print"):
            return pyhik_main.main(argv)

    def test_no_command_prints_the_usage(self):
        self.assertEqual(self.run_main([]), 2)
        self.assertEqual(self.run_main(["--help"]), 0)

    def test_unknown_command(self):
        self.assertEqual(self.run_main(["nope"]), 2)

    def test_checkisapi_gets_the_rest_of_the_arguments(self):
        with patch.object(checkisapi, "main", return_value=7) as run:
            self.assertEqual(pyhik_main.main(["checkisapi", "--write", "--path", "WDR/enabled"]), 7)
        run.assert_called_once_with(["--write", "--path", "WDR/enabled"])

    def test_checkisapi_help(self):
        with patch("sys.stdout"), self.assertRaises(SystemExit) as raised:
            pyhik_main.main(["checkisapi", "--help"])
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
