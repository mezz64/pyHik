#!/usr/bin/env python3
"""Tests for pyhik.image and the ISAPIClient image-settings methods."""

from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from pyhik.image import (
    ImageSettingType,
    parse_image_capabilities,
    parse_image_values,
)
from pyhik.isapi import (
    ISAPIAuthError,
    ISAPIClient,
    ISAPIError,
    ISAPINotFoundError,
    ISAPINotSupportedError,
    RawResponse,
)

FIXTURES = Path(__file__).parent / "fixtures"
CAPS = (FIXTURES / "image_capabilities_fixed_basic-DS-2CD2385G1.xml").read_bytes()
VALUES = (FIXTURES / "image_values_fixed_basic-DS-2CD2385G1.xml").read_bytes()


def by_path(settings):
    return {s.path: s for s in settings}


class TestParseImageCapabilities(unittest.TestCase):
    """Parsing of a real fixed-camera capabilities/values pair."""

    def setUp(self):
        self.settings = parse_image_capabilities(CAPS, VALUES)
        self.by_path = by_path(self.settings)

    def test_types(self):
        s = self.by_path
        self.assertEqual(s["WDR/mode"].type, ImageSettingType.SELECT)
        self.assertEqual(s["WDR/WDRLevel"].type, ImageSettingType.NUMBER)
        self.assertEqual(s["HLC/enabled"].type, ImageSettingType.SWITCH)

    def test_select_options_are_raw_values(self):
        self.assertEqual(self.by_path["WDR/mode"].options, ["close", "open", "auto"])
        self.assertEqual(
            self.by_path["WhiteBalance/WhiteBalanceStyle"].options[:3],
            ["manual", "auto1", "locked"],
        )

    def test_number_range(self):
        level = self.by_path["Exposure/OverexposeSuppress/DistanceLevel"]
        self.assertEqual((level.min_value, level.max_value), (1.0, 100.0))

    def test_numeric_options_stay_a_select(self):
        level = self.by_path["IrcutFilter/nightToDayFilterLevel"]
        self.assertEqual(level.type, ImageSettingType.SELECT)
        self.assertEqual(level.options, [str(n) for n in range(8)])

    def test_current_values_come_from_values_document(self):
        self.assertEqual(self.by_path["Shutter/ShutterLevel"].value, "1/1000")
        self.assertEqual(self.by_path["Color/brightnessLevel"].value, "80")

    def test_value_falls_back_to_capabilities_default(self):
        # Advanced noise reduction is inactive, so the values document omits it.
        advanced = self.by_path["NoiseReduce/AdvancedMode/FrameNoiseReduceLevel"]
        self.assertEqual(advanced.value, "19")

    def test_empty_default_is_none(self):
        self.assertIsNone(self.by_path["ImageFlip/ImageFlipStyle"].value)

    def test_structural_paths_excluded(self):
        for path in ("id", "enabled", "videoInputID"):
            self.assertNotIn(path, self.by_path)

    def test_elements_without_opt_or_range_are_not_settings(self):
        self.assertNotIn("IrcutFilter/Schedule/scheduleType", self.by_path)
        self.assertNotIn("isSupportLaserSpotManual", self.by_path)

    def test_single_option_select_is_kept(self):
        exposure = self.by_path["Exposure/ExposureType"]
        self.assertEqual(exposure.type, ImageSettingType.SELECT)
        self.assertEqual(exposure.options, ["manual"])

    def test_blc_enabled_and_mode_are_merged(self):
        mode = self.by_path["BLC/BLCMode"]
        self.assertNotIn("BLC/enabled", self.by_path)
        self.assertEqual(mode.linked_enabled_path, "BLC/enabled")
        self.assertEqual(mode.off_value, "CLOSE")
        self.assertEqual(mode.value, "AUTO")

    def test_unmerged_enabled_switches_remain(self):
        # No sibling select with an "off" option, so these stay switches.
        for path in ("HLC/enabled", "ImageFlip/enabled", "corridor/enabled"):
            self.assertEqual(self.by_path[path].type, ImageSettingType.SWITCH)

    def test_total_count(self):
        self.assertEqual(len(self.settings), 30)


class TestParseEdgeCases(unittest.TestCase):
    NS = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'

    def test_repeated_elements_are_reported_once(self):
        caps = (
            "<ImageChannel %s><Region><level min=\"0\" max=\"10\">1</level></Region>"
            "<Region><level min=\"0\" max=\"10\">2</level></Region></ImageChannel>" % self.NS
        )
        values = caps.replace(' min="0" max="10"', "")
        settings = parse_image_capabilities(caps, values)
        self.assertEqual([s.path for s in settings], ["Region/level"])
        # The first occurrence, which is also the one a write changes.
        self.assertEqual(settings[0].value, "1")
        self.assertEqual(parse_image_values(values), {"Region/level": "1"})

    def test_empty_options_are_dropped(self):
        caps = '<ImageChannel %s><X><m opt="a,,b,">a</m><n opt=",">x</n></X></ImageChannel>' % self.NS
        settings = parse_image_capabilities(caps)
        self.assertEqual([(s.path, s.options) for s in settings], [("X/m", ["a", "b"])])

    def test_off_value_and_enabled_flag_are_matched_in_any_case(self):
        for off in ("CLOSE", "close", "Close", "cLoSe"):
            for enabled in ("enabled", "Enabled"):
                with self.subTest(off=off, enabled=enabled):
                    caps = (
                        '<ImageChannel %s><F><%s opt="true,false">true</%s>'
                        '<FMode opt="AUTO,%s">AUTO</FMode></F></ImageChannel>'
                        % (self.NS, enabled, enabled, off)
                    )
                    values = (
                        "<ImageChannel %s><F><%s>false</%s></F></ImageChannel>"
                        % (self.NS, enabled, enabled)
                    )
                    settings = parse_image_capabilities(caps, values)
                    self.assertEqual([s.path for s in settings], ["F/FMode"])
                    mode = settings[0]
                    self.assertEqual(mode.linked_enabled_path, "F/%s" % enabled)
                    # The device's spelling is kept, since it is written back as is.
                    self.assertEqual((mode.off_value, mode.value), (off, off))

    def test_only_close_means_off(self):
        for opt in ("OFF,AUTO", "closed,AUTO", "disclose,AUTO", "close-up,AUTO"):
            with self.subTest(opt=opt):
                caps = (
                    '<ImageChannel %s><F><enabled opt="true,false">true</enabled>'
                    '<FMode opt="%s">AUTO</FMode></F></ImageChannel>' % (self.NS, opt)
                )
                settings = {s.path: s for s in parse_image_capabilities(caps)}
                self.assertEqual(sorted(settings), ["F/FMode", "F/enabled"])
                self.assertIsNone(settings["F/FMode"].linked_enabled_path)

    def test_enabled_value_is_read_in_any_case(self):
        caps = (
            '<ImageChannel %s><F><enabled opt="true,false">true</enabled>'
            '<FMode opt="close,AUTO">AUTO</FMode></F></ImageChannel>' % self.NS
        )
        values = (
            "<ImageChannel %s><F><enabled>TRUE</enabled><FMode>AUTO</FMode></F></ImageChannel>"
            % self.NS
        )
        self.assertEqual(parse_image_capabilities(caps, values)[0].value, "AUTO")

    def test_first_of_several_off_capable_selects_is_linked(self):
        caps = (
            '<ImageChannel %s><F><enabled opt="true,false">true</enabled>'
            '<FMode opt="CLOSE,AUTO">AUTO</FMode><other opt="close,x">x</other></F></ImageChannel>'
            % self.NS
        )
        linked = {s.path: s.linked_enabled_path for s in parse_image_capabilities(caps)}
        self.assertEqual(linked, {"F/FMode": "F/enabled", "F/other": None})


class TestParseVariants(unittest.TestCase):
    def test_disabled_linked_select_reports_off_value(self):
        values = VALUES.replace(
            b"<enabled>true</enabled>\n<BLCMode>AUTO</BLCMode>",
            b"<enabled>false</enabled>",
        )
        self.assertNotEqual(values, VALUES)  # the edit applied
        mode = by_path(parse_image_capabilities(CAPS, values))["BLC/BLCMode"]
        self.assertEqual(mode.value, "CLOSE")

    def test_without_values_document_uses_capabilities_defaults(self):
        settings = by_path(parse_image_capabilities(CAPS))
        self.assertEqual(settings["WDR/WDRLevel"].value, "41")
        self.assertEqual(settings["BLC/BLCMode"].value, "AUTO")

    def test_accepts_text_input(self):
        settings = parse_image_capabilities(CAPS.decode("utf-8"), VALUES.decode("utf-8"))
        self.assertEqual(len(settings), 30)

    def test_namespace_agnostic(self):
        other = CAPS.replace(
            b"http://www.hikvision.com/ver20/XMLSchema",
            b"http://www.isapi.org/ver20/XMLSchema",
        )
        self.assertEqual(
            by_path(parse_image_capabilities(other)).keys(),
            by_path(parse_image_capabilities(CAPS)).keys(),
        )

    def test_no_namespace(self):
        xml = b'<ImageChannel><Gain><GainLevel min="0" max="9">3</GainLevel></Gain></ImageChannel>'
        (gain,) = parse_image_capabilities(xml)
        self.assertEqual((gain.path, gain.type), ("Gain/GainLevel", ImageSettingType.NUMBER))

    def test_switch_detection_is_case_insensitive(self):
        xml = b'<C><X><enabled opt="True,False">true</enabled></X></C>'
        (switch,) = parse_image_capabilities(xml)
        self.assertEqual(switch.type, ImageSettingType.SWITCH)

    def test_non_numeric_range_is_ignored(self):
        xml = b'<C><Gain><GainLevel min="a" max="b">1</GainLevel></Gain></C>'
        self.assertEqual(parse_image_capabilities(xml), [])

    def test_invalid_xml_raises_parse_error(self):
        import xml.etree.ElementTree as ET

        with self.assertRaises(ET.ParseError):
            parse_image_capabilities(b"not xml")


class TestParseImageValues(unittest.TestCase):
    def test_flattens_leaves(self):
        values = parse_image_values(VALUES)
        self.assertEqual(values["Gain/GainLevel"], "45")
        self.assertEqual(values["NoiseReduce/GeneralMode/generalLevel"], "40")

    def test_skips_empty_and_container_elements(self):
        values = parse_image_values(VALUES)
        self.assertNotIn("LaserLight", values)
        self.assertNotIn("Gain", values)


class TestISAPIClientImage(unittest.TestCase):
    def setUp(self):
        self.client = ISAPIClient(host="192.168.1.100", username="admin", password="pw")

    def _responses(self, *responses):
        return patch.object(self.client, "request_raw", side_effect=list(responses))

    def test_get_image_settings(self):
        with self._responses(
            RawResponse(200, CAPS), RawResponse(200, VALUES)
        ) as request_raw:
            settings = self.client.get_image_settings(channel=1)
        self.assertEqual(len(settings), 30)
        self.assertEqual(
            [c.args for c in request_raw.call_args_list],
            [
                ("GET", "/ISAPI/Image/channels/1/capabilities"),
                ("GET", "/ISAPI/Image/channels/1"),
            ],
        )

    def test_channel_is_used_in_endpoint(self):
        with self._responses(
            RawResponse(200, CAPS), RawResponse(200, VALUES)
        ) as request_raw:
            self.client.get_image_settings(channel=2)
        self.assertEqual(request_raw.call_args_list[0].args[1], "/ISAPI/Image/channels/2/capabilities")

    @staticmethod
    def _status_body(sub):
        return (
            b'<ResponseStatus xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            b"<statusCode>4</statusCode><statusString>Invalid Operation</statusString>"
            b"<subStatusCode>%s</subStatusCode></ResponseStatus>" % sub.encode()
        )

    def test_status_codes_map_to_errors(self):
        cases = [
            (401, b"", ISAPIAuthError),
            (403, b"", ISAPIAuthError),
            (403, self._status_body("lowPrivilege"), ISAPIAuthError),
            (404, b"", ISAPINotSupportedError),
            (501, b"", ISAPINotSupportedError),
            (400, self._status_body("notSupport"), ISAPINotSupportedError),
            (403, self._status_body("notSupport"), ISAPINotSupportedError),
            (500, b"<html>oops</html>", ISAPIError),
            (400, self._status_body("invalidOperation"), ISAPIError),
        ]
        for status, body, error in cases:
            with self.subTest(status=status, body=body[:60]):
                with self._responses(RawResponse(status, body)):
                    with self.assertRaises(error) as ctx:
                        self.client.get_image_settings()
                self.assertEqual(ctx.exception.status_code, status)
                # Only "not supported" is reported as such.
                self.assertEqual(
                    isinstance(ctx.exception, ISAPINotSupportedError),
                    error is ISAPINotSupportedError,
                )

    def test_not_supported_is_still_a_not_found_error(self):
        with self._responses(RawResponse(404, b"")):
            with self.assertRaises(ISAPINotFoundError):
                self.client.get_image_settings()

    def test_sub_status_is_exposed_on_the_error(self):
        with self._responses(RawResponse(403, self._status_body("lowPrivilege"))):
            with self.assertRaises(ISAPIAuthError) as ctx:
                self.client.get_image_settings()
        self.assertEqual(ctx.exception.sub_status, "lowPrivilege")

    def test_error_without_a_body_has_no_sub_status(self):
        with self._responses(RawResponse(500, b"")):
            with self.assertRaises(ISAPIError) as ctx:
                self.client.get_image_values()
        self.assertIsNone(ctx.exception.sub_status)

    def test_values_endpoint_not_supported_falls_back_to_defaults(self):
        with self._responses(RawResponse(200, CAPS), RawResponse(400, self._status_body("notSupport"))):
            settings = by_path(self.client.get_image_settings())
        self.assertEqual(settings["WDR/WDRLevel"].value, "41")

    def test_missing_values_endpoint_falls_back_to_defaults(self):
        with self._responses(RawResponse(200, CAPS), RawResponse(404, b"")):
            settings = by_path(self.client.get_image_settings())
        self.assertEqual(settings["WDR/WDRLevel"].value, "41")

    def test_values_endpoint_auth_error_is_not_swallowed(self):
        with self._responses(RawResponse(200, CAPS), RawResponse(403, b"")):
            with self.assertRaises(ISAPIAuthError):
                self.client.get_image_settings()

    def test_invalid_xml_raises_isapi_error(self):
        with self._responses(RawResponse(200, b"junk"), RawResponse(200, b"junk")):
            with self.assertRaises(ISAPIError):
                self.client.get_image_settings()

    def test_get_image_values(self):
        with self._responses(RawResponse(200, VALUES)) as request_raw:
            values = self.client.get_image_values()
        self.assertEqual(values["Shutter/ShutterLevel"], "1/1000")
        request_raw.assert_called_once_with("GET", "/ISAPI/Image/channels/1")

    def test_get_image_values_invalid_xml(self):
        with self._responses(RawResponse(200, b"junk")):
            with self.assertRaises(ISAPIError):
                self.client.get_image_values()


if __name__ == "__main__":
    unittest.main()
