#!/usr/bin/env python3
"""Tests for writing image settings.

These run the real ISAPIClient against a fake device at the HTTP-session
level (not by mocking ISAPIClient methods), so the exact request bodies and
the retry sequences are exercised.
"""

from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from pyhik.image import (
    NO_SUB_STATUS,
    ImageSetting,
    ImageSettingType,
    ImageWriteResult,
    apply_image_changes,
    conflict_resolution,
    find_by_path,
    parse_image_capabilities,
    parse_response_status,
    raw_insert_after,
    raw_replace,
)
from pyhik.isapi import ISAPIAuthError, ISAPIClient, ISAPIError

FIXTURES = Path(__file__).parent / "fixtures"
CAPS = (FIXTURES / "image_capabilities_fixed_basic-DS-2CD2385G1.xml").read_bytes()
VALUES = (FIXTURES / "image_values_fixed_basic-DS-2CD2385G1.xml").read_text()

NS = "http://www.hikvision.com/ver20/XMLSchema"
IMAGE = "/ISAPI/Image/channels/1"


def response_status(ok, sub="ok"):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ResponseStatus version="2.0" xmlns="%s"><requestURL>/x</requestURL>'
        "<statusCode>%d</statusCode><statusString>%s</statusString>"
        "<subStatusCode>%s</subStatusCode></ResponseStatus>"
        % (NS, 1 if ok else 4, "OK" if ok else "Invalid Operation", sub)
    ).encode()


def get_text(xml, path):
    node = xml_find(ET.fromstring(xml.encode()), path)
    return None if node is None else node.text


def xml_find(root, path):
    from pyhik.image import find_by_path
    return find_by_path(root, path)


class FakeCamera:
    """A device that holds an ImageChannel document and enforces the
    WDR / HLC / BLC exclusivity against its *current* state."""

    def __init__(self, xml=VALUES, full_put_supported=True, hlc_wdr_code=None, hlc_on_code=None):
        self.xml = xml
        # Firmware that answers 403 with this code when HLC and WDR would both be on.
        self.hlc_wdr_code = hlc_wdr_code
        # Code for turning HLC on while WDR is on, if the firmware uses a different one.
        self.hlc_on_code = hlc_on_code or hlc_wdr_code
        self.full_put_supported = full_put_supported
        self.puts = []  # (path, body)

    def _wdr_on(self):
        return get_text(self.xml, "WDR/mode") not in (None, "close")

    def _enabled(self, path):
        return get_text(self.xml, path) == "true"

    def request(self, method, url, auth=None, params=None, data=None,
                headers=None, timeout=None):
        path = urlparse(url).path
        resp = MagicMock(headers={})
        if method == "GET":
            resp.status_code = 200
            if path == IMAGE:
                resp.content = self.xml.encode()
            else:
                top = path.rsplit("/", 1)[1]
                block = re.search(r"<%s\b.*?</%s>" % (top, top), self.xml, re.S)
                resp.content = block.group(0).encode()
            return resp

        body = data.decode()
        self.puts.append((path, body))
        if path == IMAGE and not self.full_put_supported:
            return self._reply(resp, 400, False, "notSupport")
        if path == IMAGE:
            new = body
            enabling_wdr = (
                get_text(new, "WDR/mode") not in (None, "close") and not self._wdr_on()
            )
            if self.hlc_wdr_code and enabling_wdr and self._enabled("HLC/enabled"):
                return self._reply(resp, 403, False, self.hlc_wdr_code)
            if (
                self.hlc_on_code
                and get_text(new, "HLC/enabled") == "true"
                and not self._enabled("HLC/enabled")
                and self._wdr_on()
            ):
                return self._reply(resp, 403, False, self.hlc_on_code)
            if enabling_wdr and (self._enabled("BLC/enabled") or self._enabled("HLC/enabled")):
                return self._reply(resp, 400, False, "MutexWithWDR")
            for flag in ("BLC/enabled", "HLC/enabled"):
                if get_text(new, flag) == "true" and not self._enabled(flag) and self._wdr_on():
                    return self._reply(resp, 400, False, "WDRNotDisable")
            self.xml = new
        else:
            top = path.rsplit("/", 1)[1]
            self.xml = re.sub(r"<%s\b.*?</%s>" % (top, top), lambda m: body, self.xml, count=1, flags=re.S)
        return self._reply(resp, 200, True)

    @staticmethod
    def _reply(resp, status, ok, sub="ok"):
        resp.status_code = status
        resp.content = response_status(ok, sub)
        return resp


def client_for(camera):
    client = ISAPIClient(host="192.168.1.100", username="admin", password="pw")
    client._auth = MagicMock()
    client._session = MagicMock()
    client._session.request.side_effect = camera.request
    return client


class TestRawHelpers(unittest.TestCase):
    def test_replace_is_scoped_to_parent(self):
        xml = "<R><BLC><enabled>true</enabled></BLC><HLC><enabled>true</enabled></HLC></R>"
        out = raw_replace(xml, "BLC/enabled", "true", "false")
        self.assertEqual(
            out, "<R><BLC><enabled>false</enabled></BLC><HLC><enabled>true</enabled></HLC></R>"
        )

    def test_replace_escapes_and_ignores_regex_backslashes(self):
        out = raw_replace("<R><A><b>1</b></A></R>", "A/b", "1", "x\\g<1>&y")
        self.assertEqual(out, "<R><A><b>x\\g&lt;1&gt;&amp;y</b></A></R>")

    def test_replace_preserves_attributes_on_target(self):
        out = raw_replace('<R><A><b k="v">1</b></A></R>', "A/b", "1", "2")
        self.assertEqual(out, '<R><A><b k="v">2</b></A></R>')

    def test_replace_unchanged_when_not_found(self):
        xml = "<R><A><b/></A></R>"
        self.assertEqual(raw_replace(xml, "A/b", "", "2"), xml)
        self.assertEqual(raw_replace(xml, "A/zz", "", "2"), xml)

    def test_replace_skips_a_nested_element_with_the_same_name_and_value(self):
        xml = (
            "<R><SupplementLight><EventIntelligenceModeCfg><whiteLightBrightness>50"
            "</whiteLightBrightness></EventIntelligenceModeCfg><whiteLightBrightness>50"
            "</whiteLightBrightness></SupplementLight></R>"
        )
        out = raw_replace(xml, "SupplementLight/whiteLightBrightness", "50", "80")
        self.assertEqual(
            out, xml.replace("<whiteLightBrightness>50</whiteLightBrightness></Supp",
                             "<whiteLightBrightness>80</whiteLightBrightness></Supp")
        )

    def test_replace_follows_the_whole_path(self):
        # A parent with the same name earlier in another branch is not used.
        xml = (
            "<R><Other><Schedule><scheduleType>day</scheduleType></Schedule></Other>"
            "<IrcutFilter><Schedule><scheduleType>day</scheduleType></Schedule></IrcutFilter></R>"
        )
        out = raw_replace(xml, "IrcutFilter/Schedule/scheduleType", "day", "night")
        self.assertEqual(out, xml[: xml.rindex("day")] + "night" + xml[xml.rindex("day") + 3 :])

    def test_replace_uses_the_first_element_at_each_level_like_find_by_path(self):
        xml = "<R><A><c>1</c></A><A><b>1</b></A></R>"
        self.assertIsNone(find_by_path(ET.fromstring(xml), "A/b"))
        self.assertEqual(raw_replace(xml, "A/b", "1", "2"), xml)

    def test_replace_handles_entities_comments_prefixes_and_attributes(self):
        xml = (
            '<?xml version="1.0"?><!-- <b>1</b> --><R><A note="a>b"><b>x&apos;y</b>'
            "</A><h:B><h:c>1</h:c></h:B></R>"
        )
        out = raw_replace(xml, "A/b", "x'y", "z")
        self.assertIn('<A note="a>b"><b>z</b>', out)
        self.assertIn("<!-- <b>1</b> -->", out)
        self.assertIn("<h:c>2</h:c>", raw_replace(xml, "B/c", "1", "2"))

    def test_replace_leaves_elements_with_children_or_other_text_alone(self):
        xml = "<R><A><b><c>1</c></b></A></R>"
        self.assertEqual(raw_replace(xml, "A/b", "", "2"), xml)
        self.assertEqual(raw_replace("<R><A><b>1</b></A></R>", "A/b", "2", "3"), "<R><A><b>1</b></A></R>")

    def test_insert_after(self):
        xml = "<R><BLC><enabled>true</enabled></BLC></R>"
        out = raw_insert_after(xml, "BLC/enabled", "BLC/BLCMode", "UP")
        self.assertEqual(out, "<R><BLC><enabled>true</enabled>\n<BLCMode>UP</BLCMode></BLC></R>")

    def test_insert_after_bare_name_in_sub_endpoint_document(self):
        xml = '<BLC xmlns="x"><enabled>true</enabled></BLC>'
        out = raw_insert_after(xml, "enabled", "BLCMode", "UP")
        self.assertEqual(out, '<BLC xmlns="x"><enabled>true</enabled>\n<BLCMode>UP</BLCMode></BLC>')

    def test_insert_after_missing_sibling_is_unchanged(self):
        xml = "<R><BLC></BLC></R>"
        self.assertEqual(raw_insert_after(xml, "BLC/enabled", "BLC/BLCMode", "UP"), xml)

    def test_apply_reports_missing_and_self_closing(self):
        xml = "<R><A><b/></A></R>"
        edited, missing = apply_image_changes(xml, {"A/b": "1", "A/zz": "1"})
        self.assertEqual(edited, xml)
        self.assertEqual(sorted(missing), ["A/b", "A/zz"])

    def test_apply_skips_unchanged_values(self):
        edited, missing = apply_image_changes(VALUES, {"Gain/GainLevel": "45"})
        self.assertEqual((edited, missing), (VALUES, []))


class TestParseResponseStatus(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(parse_response_status(response_status(True), 200).success)

    def test_http_error_with_sub_status(self):
        result = parse_response_status(response_status(False, "WDRNotDisable"), 400)
        self.assertEqual((result.success, result.sub_status), (False, "WDRNotDisable"))

    def test_other_namespace(self):
        body = response_status(True).replace(NS.encode(), b"http://www.isapi.org/ver20/XMLSchema")
        self.assertTrue(parse_response_status(body, 200).success)

    def test_not_xml(self):
        result = parse_response_status(b"<html>nope", 200)
        self.assertEqual((result.success, result.sub_status), (False, "invalidResponse"))

    def test_200_with_error_status_is_failure(self):
        self.assertFalse(parse_response_status(response_status(False, "deviceError"), 200).success)

    def test_reboot_required_is_success(self):
        body = (
            b'<ResponseStatus version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            b"<statusCode>7</statusCode><statusString>Reboot Required</statusString>"
            b"<subStatusCode>rebootRequired</subStatusCode></ResponseStatus>"
        )
        result = parse_response_status(body, 200)
        self.assertEqual((result.success, result.sub_status), (True, "rebootRequired"))

    def test_empty_200_is_success(self):
        for body in (b"", b"  \n"):
            with self.subTest(body=body):
                result = parse_response_status(body, 200)
                self.assertEqual((result.success, result.sub_status), (True, "ok"))
        self.assertFalse(parse_response_status(b"", 400).success)

    def test_permission_message(self):
        self.assertIn("permission", ImageWriteResult(False, 403, "lowPrivilege").error_description)

    def test_error_description_does_not_blame_privileges_for_conflicts(self):
        text = ImageWriteResult(False, 403, "HLCMutexWDROrDehaze").error_description
        self.assertIn("HLCMutexWDROrDehaze", text)
        self.assertTrue(text.startswith("WDR or defog"))
        self.assertIn("HLC (Highlight Compensation)", text)
        self.assertNotIn("permission", text)
        text = ImageWriteResult(False, 403, "WDROrDehazeMutexHLC").error_description
        self.assertTrue(text.startswith("HLC (Highlight Compensation) cannot be turned on"))
        self.assertIn("WDROrDehazeMutexHLC", text)
        self.assertNotIn("permission", text)
        # An unknown sub status on a 403 is reported as is.
        self.assertEqual(ImageWriteResult(False, 403, "somethingNew").error_description, "somethingNew")
        # No sub status at all on a 403 is still read as a rights problem.
        self.assertIn("permission", ImageWriteResult(False, 403, NO_SUB_STATUS).error_description)


class TestPutImageSettings(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.client = client_for(self.camera)

    def test_only_the_target_text_changes(self):
        result = self.client.set_image_setting("Shutter/ShutterLevel", "1/500")
        self.assertTrue(result.success)
        self.assertEqual(
            self.camera.xml,
            VALUES.replace("<ShutterLevel>1/1000</ShutterLevel>", "<ShutterLevel>1/500</ShutterLevel>"),
        )
        self.assertEqual(len(self.camera.puts), 1)
        self.assertEqual(self.camera.puts[0][0], IMAGE)

    def test_same_path_name_in_other_sections_is_untouched(self):
        self.client.set_image_setting("BLC/enabled", "false")
        self.assertEqual(get_text(self.camera.xml, "BLC/enabled"), "false")
        self.assertEqual(get_text(self.camera.xml, "HLC/enabled"), "true")
        self.assertEqual(self.camera.xml.count("xmlns="), VALUES.count("xmlns="))

    def test_unchanged_value_succeeds(self):
        self.assertTrue(self.client.set_image_setting("Gain/GainLevel", "45").success)
        self.assertEqual(self.camera.xml, VALUES)

    def test_unknown_path_sends_nothing(self):
        result = self.client.set_image_setting("Nope/Missing", "1")
        self.assertEqual((result.success, result.sub_status), (False, "pathNotFound"))
        self.assertEqual(self.camera.puts, [])

    def test_missing_paths_are_skipped_when_others_apply(self):
        result = self.client.put_image_settings({"Gain/GainLevel": "50", "Nope/x": "1"})
        self.assertTrue(result.success)
        self.assertEqual(get_text(self.camera.xml, "Gain/GainLevel"), "50")

    def test_invalid_document_raises(self):
        self.camera.xml = "<not closed"
        with self.assertRaises(ISAPIError):
            self.client.set_image_setting("Gain/GainLevel", "1")

    def test_unresolvable_error_is_returned_after_one_attempt(self):
        original = self.camera.request

        def forbidden(method, url, **kw):
            if method == "PUT":
                return FakeCamera._reply(MagicMock(headers={}), 403, False, "lowPrivilege")
            return original(method, url, **kw)

        self.client._session.request.side_effect = forbidden
        result = self.client.set_image_setting("Gain/GainLevel", "1")
        self.assertEqual((result.success, result.sub_status, result.status_code), (False, "lowPrivilege", 403))


class TestConflicts(unittest.TestCase):
    def test_enabling_wdr_switches_off_blc_and_hlc_first(self):
        camera = FakeCamera()
        result = client_for(camera).set_image_setting("WDR/mode", "open")
        self.assertTrue(result.success)
        self.assertEqual(get_text(camera.xml, "WDR/mode"), "open")
        self.assertEqual(get_text(camera.xml, "BLC/enabled"), "false")
        self.assertEqual(get_text(camera.xml, "HLC/enabled"), "false")
        self.assertEqual(len(camera.puts), 3)  # rejected, disable blockers, retry

    def test_enabling_hlc_switches_off_wdr_first(self):
        xml = VALUES.replace("<mode>close</mode>", "<mode>open</mode>", 1)
        xml = xml.replace("<enabled>true</enabled>\n<HLCLevel>", "<enabled>false</enabled>\n<HLCLevel>")
        camera = FakeCamera(xml)
        self.assertEqual(get_text(camera.xml, "WDR/mode"), "open")
        result = client_for(camera).set_image_setting("HLC/enabled", "true")
        self.assertTrue(result.success)
        self.assertEqual(get_text(camera.xml, "WDR/mode"), "close")
        self.assertEqual(get_text(camera.xml, "HLC/enabled"), "true")

    def test_gives_up_after_retries(self):
        camera = FakeCamera()
        client = client_for(camera)
        original = camera.request

        def always_conflict(method, url, **kw):
            if method == "PUT" and urlparse(url).path == IMAGE:
                body = kw["data"].decode()
                if get_text(body, "WDR/mode") == "open":
                    return FakeCamera._reply(MagicMock(headers={}), 400, False, "MutexWithWDR")
            return original(method, url, **kw)

        client._session.request.side_effect = always_conflict
        result = client.set_image_setting("WDR/mode", "open")
        self.assertFalse(result.success)
        self.assertLessEqual(len(camera.puts), 1 + 2 * 2)


class StubbornCamera(FakeCamera):
    """Keeps refusing WDR with MutexWithWDR, whatever is switched off."""

    def request(self, method, url, **kwargs):
        data = kwargs.get("data")
        if method == "PUT" and get_text(data.decode(), "WDR/mode") == "open":
            self.puts.append((urlparse(url).path, data.decode()))
            return self._reply(MagicMock(headers={}), 400, False, "MutexWithWDR")
        return super().request(method, url, **kwargs)


class TestConflictEdgeCases(unittest.TestCase):
    def test_resolution_is_a_copy(self):
        conflict_resolution("WDRNotDisable", "BLC/enabled")["WDR/mode"] = "open"
        conflict_resolution("MutexWithWDR", "WDR/mode")["BLC/enabled"] = "true"
        self.assertEqual(conflict_resolution("WDRNotDisable", "BLC/enabled"), {"WDR/mode": "close"})
        self.assertEqual(
            conflict_resolution("MutexWithWDR", "WDR/mode"),
            {"BLC/enabled": "false", "HLC/enabled": "false"},
        )

    def test_mutex_with_wdr_path_is_matched_in_any_case(self):
        self.assertEqual(
            conflict_resolution("MutexWithWDR", "wdr/MODE"),
            {"BLC/enabled": "false", "HLC/enabled": "false"},
        )

    def test_nothing_to_switch_off_stops_without_retrying(self):
        camera = StubbornCamera()
        camera.xml = raw_replace(camera.xml, "BLC/enabled", "true", "false")
        camera.xml = raw_replace(camera.xml, "HLC/enabled", "true", "false")
        with self.assertLogs("pyhik.isapi", "WARNING") as logs:
            result = client_for(camera).set_image_setting("WDR/mode", "open")
        self.assertEqual((result.success, result.sub_status), (False, "MutexWithWDR"))
        self.assertEqual(len(camera.puts), 1)
        self.assertEqual(result.also_changed, {})
        self.assertIn("Setting WDR/mode failed: MutexWithWDR", logs.output[-1])

    def test_failure_after_the_last_retry_is_logged(self):
        camera = StubbornCamera()
        with self.assertLogs("pyhik.isapi", "WARNING") as logs:
            result = client_for(camera).set_image_setting("WDR/mode", "open")
        self.assertFalse(result.success)
        self.assertEqual(result.sub_status, "MutexWithWDR")
        self.assertIn("Setting WDR/mode failed: MutexWithWDR", logs.output[-1])
        self.assertEqual(result.also_changed, {"BLC/enabled": "false", "HLC/enabled": "false"})


class TestHlcMutexConflicts(unittest.TestCase):
    """Firmware that answers 403 HLCMutexWDROrDehaze instead of MutexWithWDR."""

    CODE = "HLCMutexWDROrDehaze"

    def test_enabling_wdr_switches_off_hlc_first(self):
        camera = FakeCamera(hlc_wdr_code=self.CODE)
        camera.xml = raw_replace(camera.xml, "BLC/enabled", "true", "false")
        result = client_for(camera).set_image_setting("WDR/mode", "open")
        self.assertTrue(result.success)
        self.assertEqual(get_text(camera.xml, "WDR/mode"), "open")
        self.assertEqual(get_text(camera.xml, "HLC/enabled"), "false")
        self.assertEqual(len(camera.puts), 3)  # rejected, switch HLC off, retry
        self.assertEqual(result.also_changed, {"HLC/enabled": "false"})

    def test_enabling_hlc_switches_off_wdr_first(self):
        xml = VALUES.replace("<mode>close</mode>", "<mode>open</mode>", 1)
        camera = FakeCamera(xml, hlc_wdr_code=self.CODE)
        camera.xml = camera.xml.replace(
            "<enabled>true</enabled>\n<HLCLevel>", "<enabled>false</enabled>\n<HLCLevel>"
        )
        result = client_for(camera).set_image_setting("HLC/enabled", "true")
        self.assertTrue(result.success)
        self.assertEqual(get_text(camera.xml, "WDR/mode"), "close")
        self.assertEqual(get_text(camera.xml, "HLC/enabled"), "true")
        self.assertEqual(result.also_changed, {"WDR/mode": "close"})

    def test_blockers_already_off_are_not_written(self):
        xml = VALUES.replace("<mode>close</mode>", "<mode>open</mode>", 1)
        camera = FakeCamera(xml, hlc_wdr_code=self.CODE)
        camera.xml = camera.xml.replace(
            "<enabled>true</enabled>\n<HLCLevel>", "<enabled>false</enabled>\n<HLCLevel>"
        )
        before = camera.xml
        client_for(camera).set_image_setting("HLC/enabled", "true")
        # rejected, switch WDR off (the fixture has no defog), retry
        self.assertEqual(len(camera.puts), 3)
        prerequisite = camera.puts[1][1]
        self.assertEqual(
            prerequisite, raw_replace(before, "WDR/mode", "open", "close")
        )

    def test_firmware_with_a_code_for_each_direction(self):
        # DS-2CD2385G1: HLCMutexWDROrDehaze for WDR on, WDROrDehazeMutexHLC for HLC on.
        other = "WDROrDehazeMutexHLC"
        camera = FakeCamera(hlc_wdr_code=self.CODE, hlc_on_code=other)
        camera.xml = raw_replace(camera.xml, "BLC/enabled", "true", "false")
        client = client_for(camera)
        self.assertTrue(client.set_image_setting("WDR/mode", "open").success)
        self.assertEqual(get_text(camera.xml, "HLC/enabled"), "false")
        self.assertTrue(client.set_image_setting("HLC/enabled", "true").success)
        self.assertEqual(get_text(camera.xml, "WDR/mode"), "close")
        self.assertEqual(get_text(camera.xml, "HLC/enabled"), "true")
        self.assertEqual(conflict_resolution(other, "HLC/enabled"), conflict_resolution(self.CODE, "HLC/enabled"))
        self.assertEqual(conflict_resolution(other, "WDR/mode"), {"HLC/enabled": "false"})
        self.assertIsNone(conflict_resolution(other, "Gain/GainLevel"))

    def test_unrelated_setting_is_not_resolved(self):
        camera = FakeCamera(hlc_wdr_code=self.CODE)
        original = camera.request

        def reject_everything(method, url, **kw):
            if method == "PUT":
                return FakeCamera._reply(MagicMock(headers={}), 403, False, self.CODE)
            return original(method, url, **kw)

        client = client_for(camera)
        client._session.request.side_effect = reject_everything
        result = client.set_image_setting("Gain/GainLevel", "50")
        self.assertEqual((result.success, result.sub_status), (False, self.CODE))
        self.assertEqual(get_text(camera.xml, "HLC/enabled"), "true")
        self.assertIn("HLC (Highlight Compensation)", result.error_description)

    def test_resolution_table(self):
        self.assertEqual(conflict_resolution(self.CODE, "WDR/mode"), {"HLC/enabled": "false"})
        self.assertEqual(conflict_resolution(self.CODE, "dehaze/dehazemode"), {"HLC/enabled": "false"})
        self.assertEqual(
            conflict_resolution(self.CODE, "HLC/enabled"),
            {"WDR/mode": "close", "Dehaze/DehazeMode": "close"},
        )
        self.assertIsNone(conflict_resolution(self.CODE, "Shutter/ShutterLevel"))


    def test_hlc_mode_linked_to_its_enabled_flag_switches_off_wdr_first(self):
        # Firmware with HLC/enabled and HLC/HLCMode (close, open, auto): the two are
        # one linked select, written through set_image_linked_setting.
        xml = VALUES.replace("<mode>close</mode>", "<mode>open</mode>", 1).replace(
            "<enabled>true</enabled>\n<HLCLevel>",
            "<enabled>false</enabled>\n<HLCMode>close</HLCMode>\n<HLCLevel>",
        )
        camera = FakeCamera(xml, hlc_wdr_code=self.CODE, hlc_on_code="WDROrDehazeMutexHLC")
        hlc_mode = ImageSetting(
            "HLC/HLCMode",
            ImageSettingType.SELECT,
            options=["close", "open", "auto"],
            linked_enabled_path="HLC/enabled",
            off_value="close",
        )
        result = client_for(camera).set_image_linked_setting(hlc_mode, "auto")
        self.assertTrue(result.success)
        self.assertEqual(get_text(camera.xml, "WDR/mode"), "close")
        self.assertEqual(get_text(camera.xml, "HLC/enabled"), "true")
        self.assertEqual(get_text(camera.xml, "HLC/HLCMode"), "auto")
        self.assertEqual(result.also_changed, {"WDR/mode": "close"})
        self.assertEqual(
            conflict_resolution("HLCMutexWDROrDehaze", "hlc/hlcmode"),
            conflict_resolution("HLCMutexWDROrDehaze", "HLC/enabled"),
        )

class TestAlsoChanged(unittest.TestCase):
    """ImageWriteResult.also_changed lists what was switched off to make room."""

    def test_plain_write_changes_nothing_else(self):
        result = client_for(FakeCamera()).set_image_setting("Shutter/ShutterLevel", "1/500")
        self.assertEqual(result.also_changed, {})

    def test_only_blockers_that_were_on_are_listed(self):
        # Enabling WDR while BLC is on and HLC is already off: only BLC is switched off.
        camera = FakeCamera()
        camera.xml = camera.xml.replace(
            "<enabled>true</enabled>\n<HLCLevel>", "<enabled>false</enabled>\n<HLCLevel>"
        )
        self.assertEqual(get_text(camera.xml, "HLC/enabled"), "false")
        self.assertEqual(get_text(camera.xml, "BLC/enabled"), "true")
        result = client_for(camera).set_image_setting("WDR/mode", "open")
        self.assertTrue(result.success)
        self.assertEqual(result.also_changed, {"BLC/enabled": "false"})

    def test_reported_on_a_failure_after_a_blocker_was_switched_off(self):
        camera = FakeCamera(hlc_wdr_code="HLCMutexWDROrDehaze")
        original = camera.request

        def refuse_wdr(method, url, **kw):
            if method == "PUT" and "<mode>open</mode>" in kw["data"].decode():
                return FakeCamera._reply(MagicMock(headers={}), 403, False, "HLCMutexWDROrDehaze")
            return original(method, url, **kw)

        client = client_for(camera)
        client._session.request.side_effect = refuse_wdr
        camera.xml = raw_replace(camera.xml, "BLC/enabled", "true", "false")
        result = client.set_image_setting("WDR/mode", "open")
        self.assertFalse(result.success)
        self.assertEqual(result.also_changed, {"HLC/enabled": "false"})


class TestSubEndpointFallback(unittest.TestCase):
    def test_single_change_retries_on_sub_endpoint(self):
        camera = FakeCamera(full_put_supported=False)
        result = client_for(camera).set_image_setting("Shutter/ShutterLevel", "1/500")
        self.assertTrue(result.success)
        self.assertEqual([p for p, _ in camera.puts], [IMAGE, IMAGE + "/Shutter"])
        self.assertEqual(get_text(camera.xml, "Shutter/ShutterLevel"), "1/500")

    def test_multiple_changes_do_not_fall_back(self):
        camera = FakeCamera(full_put_supported=False)
        result = client_for(camera).put_image_settings(
            {"Shutter/ShutterLevel": "1/500", "Gain/GainLevel": "10"}
        )
        self.assertEqual(result.sub_status, "notSupport")
        self.assertEqual(len(camera.puts), 1)

    def test_top_level_setting_has_no_fallback(self):
        camera = FakeCamera(full_put_supported=False)
        xml = VALUES.replace("</ImageChannel>", "<enableImageLossDetection>false</enableImageLossDetection>\n</ImageChannel>")
        camera.xml = xml
        result = client_for(camera).put_image_settings({"enableImageLossDetection": "true"})
        self.assertEqual(result.sub_status, "notSupport")

    def test_changes_in_one_feature_fall_back_together(self):
        camera = FakeCamera(full_put_supported=False)
        result = client_for(camera).put_image_settings(
            {"BLC/enabled": "false", "BLC/BLCMode": "CLOSE"}
        )
        self.assertTrue(result.success)
        self.assertEqual([p for p, _ in camera.puts], [IMAGE, IMAGE + "/BLC"])
        self.assertEqual(get_text(camera.xml, "BLC/enabled"), "false")
        self.assertEqual(get_text(camera.xml, "BLC/BLCMode"), "CLOSE")


class TestLinkedSettings(unittest.TestCase):
    def setUp(self):
        self.blc = {
            s.path: s for s in parse_image_capabilities(CAPS, VALUES.encode())
        }["BLC/BLCMode"]

    def test_change_mode_while_enabled(self):
        camera = FakeCamera()
        result = client_for(camera).set_image_linked_setting(self.blc, "UP")
        self.assertTrue(result.success)
        self.assertEqual(get_text(camera.xml, "BLC/BLCMode"), "UP")
        self.assertEqual(get_text(camera.xml, "BLC/enabled"), "true")

    def test_off_disables_and_sets_mode(self):
        camera = FakeCamera()
        result = client_for(camera).set_image_linked_setting(self.blc, "CLOSE")
        self.assertTrue(result.success)
        self.assertEqual(get_text(camera.xml, "BLC/enabled"), "false")
        self.assertEqual(get_text(camera.xml, "BLC/BLCMode"), "CLOSE")

    def test_off_when_mode_element_is_absent(self):
        xml = VALUES.replace("<enabled>true</enabled>\n<BLCMode>AUTO</BLCMode>", "<enabled>false</enabled>")
        camera = FakeCamera(xml)
        result = client_for(camera).set_image_linked_setting(self.blc, "CLOSE")
        self.assertTrue(result.success)

    def test_off_falls_back_to_sub_endpoint(self):
        camera = FakeCamera(full_put_supported=False)
        result = client_for(camera).set_image_linked_setting(self.blc, "CLOSE")
        self.assertTrue(result.success)
        self.assertEqual(camera.puts[-1][0], IMAGE + "/BLC")
        self.assertEqual(get_text(camera.xml, "BLC/enabled"), "false")

    def test_enable_inserts_missing_mode_element(self):
        xml = VALUES.replace("<enabled>true</enabled>\n<BLCMode>AUTO</BLCMode>", "<enabled>false</enabled>")
        camera = FakeCamera(xml)
        result = client_for(camera).set_image_linked_setting(self.blc, "AUTO")
        self.assertTrue(result.success)
        self.assertIn("<enabled>true</enabled>\n<BLCMode>AUTO</BLCMode>", camera.xml)

    def test_enable_resolves_wdr_conflict(self):
        xml = VALUES.replace("<enabled>true</enabled>\n<BLCMode>AUTO</BLCMode>", "<enabled>false</enabled>")
        xml = xml.replace("<mode>close</mode>", "<mode>open</mode>", 1)
        camera = FakeCamera(xml)
        result = client_for(camera).set_image_linked_setting(self.blc, "AUTO")
        self.assertTrue(result.success)
        self.assertEqual(get_text(camera.xml, "WDR/mode"), "close")
        self.assertEqual(get_text(camera.xml, "BLC/enabled"), "true")

    def test_enable_falls_back_to_sub_endpoint(self):
        xml = VALUES.replace("<enabled>true</enabled>\n<BLCMode>AUTO</BLCMode>", "<enabled>false</enabled>")
        camera = FakeCamera(xml, full_put_supported=False)
        result = client_for(camera).set_image_linked_setting(self.blc, "UP")
        self.assertTrue(result.success)
        self.assertEqual(camera.puts[-1][0], IMAGE + "/BLC")
        self.assertIn("<BLCMode>UP</BLCMode>", camera.xml)

    def test_unlinked_setting_is_written_normally(self):
        camera = FakeCamera()
        scene = {s.path: s for s in parse_image_capabilities(CAPS, VALUES.encode())}["Scene/mode"]
        self.assertTrue(client_for(camera).set_image_linked_setting(scene, "indoor").success)
        self.assertEqual(get_text(camera.xml, "Scene/mode"), "indoor")

    def test_missing_enabled_flag(self):
        camera = FakeCamera(VALUES.replace("<BLC ", "<BLCX ").replace("</BLC>", "</BLCX>"))
        result = client_for(camera).set_image_linked_setting(self.blc, "UP")
        self.assertEqual(result.sub_status, "pathNotFound")


HTML_ERROR_PAGE = b"<html><head><title>401</title></head><body>Unauthorized</body></html>"


class RejectingCamera(FakeCamera):
    """A FakeCamera that answers chosen requests with a fixed reply.

    ``rules`` maps ``(method, path)`` to ``(status, body)`` or to a list of
    them, used one per request (the last one repeats). The device-info check
    that follows a 401 is rejected too, so the client does not switch
    authentication and retry.
    """

    def __init__(self, rules, **kwargs):
        super().__init__(**kwargs)
        self.rules = {key: list(v) if isinstance(v, list) else [v] for key, v in rules.items()}
        self.calls = []

    def request(self, method, url, **kwargs):
        path = urlparse(url).path
        self.calls.append((method, path))
        if path == "/ISAPI/System/deviceInfo":
            return MagicMock(status_code=401, content=HTML_ERROR_PAGE, headers={})
        queue = self.rules.get((method, path))
        if queue:
            status, body = queue.pop(0) if len(queue) > 1 else queue[0]
            resp = MagicMock(headers={}, status_code=status, content=body)
            resp.text = body.decode()
            resp.ok = status < 400
            return resp
        return super().request(method, url, **kwargs)


class TestUnauthorizedWrites(unittest.TestCase):
    """A 401 on a write raises ISAPIAuthError, as it does on a read."""

    def assert_auth_error(self, call, camera):
        with self.assertRaises(ISAPIAuthError) as ctx:
            call()
        self.assertEqual(ctx.exception.status_code, 401)
        return ctx.exception

    def test_put_with_html_body(self):
        camera = RejectingCamera({("PUT", IMAGE): (401, HTML_ERROR_PAGE)})
        self.assert_auth_error(
            lambda: client_for(camera).put_image_settings({"Shutter/ShutterLevel": "1/500"}),
            camera,
        )
        self.assertEqual(camera.xml, VALUES)

    def test_put_with_response_status_body(self):
        camera = RejectingCamera({("PUT", IMAGE): (401, response_status(False, "badAuthorization"))})
        err = self.assert_auth_error(
            lambda: client_for(camera).put_image_settings({"Shutter/ShutterLevel": "1/500"}),
            camera,
        )
        self.assertEqual(err.sub_status, "badAuthorization")

    def test_put_with_empty_body(self):
        camera = RejectingCamera({("PUT", IMAGE): (401, b"")})
        self.assert_auth_error(
            lambda: client_for(camera).set_image_setting("Shutter/ShutterLevel", "1/500"), camera
        )

    def test_set_image_setting_does_not_retry(self):
        camera = RejectingCamera({("PUT", IMAGE): (401, HTML_ERROR_PAGE)})
        self.assert_auth_error(
            lambda: client_for(camera).set_image_setting("WDR/mode", "open"), camera
        )
        self.assertEqual(camera.calls.count(("PUT", IMAGE)), 1)

    def test_enable_and_mode(self):
        xml = VALUES.replace("<enabled>true</enabled>\n<BLCMode>AUTO</BLCMode>", "<enabled>false</enabled>")
        camera = RejectingCamera({("PUT", IMAGE): (401, HTML_ERROR_PAGE)}, xml=xml)
        self.assert_auth_error(
            lambda: client_for(camera).put_image_setting_with_enable(
                "BLC/enabled", "BLC/BLCMode", "AUTO"
            ),
            camera,
        )

    def test_linked_setting_off_and_on(self):
        blc = {s.path: s for s in parse_image_capabilities(CAPS, VALUES.encode())}["BLC/BLCMode"]
        for value in (blc.off_value, "AUTO"):
            with self.subTest(value=value):
                camera = RejectingCamera({("PUT", IMAGE): (401, HTML_ERROR_PAGE)})
                self.assert_auth_error(
                    lambda: client_for(camera).set_image_linked_setting(blc, value), camera
                )

    def test_sub_endpoint_get_in_put_image_settings(self):
        camera = RejectingCamera(
            {("GET", IMAGE + "/Shutter"): (401, HTML_ERROR_PAGE)}, full_put_supported=False
        )
        self.assert_auth_error(
            lambda: client_for(camera).set_image_setting("Shutter/ShutterLevel", "1/500"), camera
        )
        self.assertEqual([p for m, p in camera.calls if m == "PUT"], [IMAGE])

    def test_sub_endpoint_get_in_enable_and_mode(self):
        xml = VALUES.replace("<enabled>true</enabled>\n<BLCMode>AUTO</BLCMode>", "<enabled>false</enabled>")
        camera = RejectingCamera(
            {("GET", IMAGE + "/BLC"): (401, HTML_ERROR_PAGE)}, xml=xml, full_put_supported=False
        )
        self.assert_auth_error(
            lambda: client_for(camera).put_image_setting_with_enable(
                "BLC/enabled", "BLC/BLCMode", "UP"
            ),
            camera,
        )

    def test_sub_endpoint_put(self):
        camera = RejectingCamera(
            {("PUT", IMAGE + "/Shutter"): (401, HTML_ERROR_PAGE)}, full_put_supported=False
        )
        self.assert_auth_error(
            lambda: client_for(camera).set_image_setting("Shutter/ShutterLevel", "1/500"), camera
        )

    def test_401_while_switching_off_a_blocker(self):
        camera = RejectingCamera(
            {("PUT", IMAGE): [(400, response_status(False, "MutexWithWDR")), (401, HTML_ERROR_PAGE)]},
        )
        self.assert_auth_error(
            lambda: client_for(camera).set_image_setting("WDR/mode", "open"), camera
        )

    def test_403_stays_a_failed_result(self):
        camera = RejectingCamera({("PUT", IMAGE): (403, response_status(False, "lowPrivilege"))})
        result = client_for(camera).put_image_settings({"Shutter/ShutterLevel": "1/500"})
        self.assertEqual((result.success, result.status_code, result.sub_status), (False, 403, "lowPrivilege"))


class TestFailureWithoutSubStatus(unittest.TestCase):
    def test_error_page_is_not_ok(self):
        for status in (400, 403, 404, 500, 503):
            with self.subTest(status=status):
                result = parse_response_status(HTML_ERROR_PAGE, status)
                self.assertEqual((result.success, result.sub_status), (False, NO_SUB_STATUS))
                self.assertNotEqual(result.error_description, "ok")
                self.assertIn("HTTP %d" % status, result.error_description)

    def test_401_body_is_not_ok(self):
        result = parse_response_status(HTML_ERROR_PAGE, 401)
        self.assertEqual((result.success, result.sub_status), (False, NO_SUB_STATUS))

    def test_failure_status_without_sub_status_on_200(self):
        body = (
            b'<ResponseStatus xmlns="%s"><statusCode>3</statusCode>'
            b"<statusString>Device Error</statusString></ResponseStatus>" % NS.encode()
        )
        result = parse_response_status(body, 200)
        self.assertEqual((result.success, result.sub_status), (False, NO_SUB_STATUS))

    def test_ok_only_for_success(self):
        body = (
            b'<ResponseStatus xmlns="%s"><statusCode>1</statusCode>'
            b"<statusString>OK</statusString></ResponseStatus>" % NS.encode()
        )
        result = parse_response_status(body, 200)
        self.assertEqual((result.success, result.sub_status), (True, "ok"))
        self.assertEqual(parse_response_status(b"", 200).sub_status, "ok")

    def test_a_device_sub_status_is_kept(self):
        result = parse_response_status(response_status(False, "WDRNotDisable"), 400)
        self.assertEqual(result.sub_status, "WDRNotDisable")

    def test_403_without_sub_status_reads_as_permission_denied(self):
        description = parse_response_status(HTML_ERROR_PAGE, 403).error_description
        self.assertIn("permission denied", description)
        self.assertIn("HTTP 403", description)

    def test_not_a_conflict_so_nothing_is_resolved(self):
        self.assertIsNone(conflict_resolution(NO_SUB_STATUS, "WDR/mode"))

    def test_error_page_on_put_is_a_failed_result(self):
        camera = RejectingCamera({("PUT", IMAGE): (500, HTML_ERROR_PAGE)})
        with self.assertLogs("pyhik.isapi", "WARNING") as logs:
            result = client_for(camera).set_image_setting("Shutter/ShutterLevel", "1/500")
        self.assertEqual((result.success, result.status_code, result.sub_status), (False, 500, NO_SUB_STATUS))
        self.assertIn("HTTP 500", logs.output[0])
        self.assertNotIn("failed: ok", logs.output[0])


if __name__ == "__main__":
    unittest.main()
