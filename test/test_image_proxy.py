#!/usr/bin/env python3
"""Tests for image settings of an NVR's attached cameras (ImageProxy).

The fixtures here are written from Hikvision's NVR ISAPI guide (namespace
and element names), not captured from a device. The per-channel document is
the same ImageChannel document a camera serves, so the camera fixture is
reused for it.
"""

from pathlib import Path
import unittest
from unittest.mock import MagicMock
from urllib.parse import urlparse

from pyhik.image import NO_SUB_STATUS, ImageChannelInfo, parse_image_channels
from pyhik.isapi import (
    ISAPIAuthError,
    ISAPIClient,
    ISAPIError,
    ISAPINotFoundError,
    ISAPINotSupportedError,
    RawResponse,
)
from test.test_image_write import (
    HTML_ERROR_PAGE,
    VALUES,
    FakeCamera,
    RejectingCamera,
    client_for,
    get_text,
)

PROXY = "/ISAPI/ContentMgmt/ImageProxy/channels"
DIRECT = "/ISAPI/Image/channels"

NVR_LIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<ImageChannellist xmlns="http://www.isapi.org/ver20/XMLSchema" version="2.0">
<ImageChannel><id>1</id><enabled>true</enabled><inputPort>1</inputPort></ImageChannel>
<ImageChannel><id>2</id><enabled>true</enabled><inputPort>1</inputPort></ImageChannel>
<ImageChannel><id>3</id><enabled>false</enabled></ImageChannel>
</ImageChannellist>"""

CAMERA_LIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<ImageChannellist xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
<ImageChannel><id>1</id><enabled>true</enabled><videoInputID>1</videoInputID></ImageChannel>
</ImageChannellist>"""


def sent(client):
    """(method, path) of every request the client made to the fake device."""
    return [
        (c.args[0], urlparse(c.args[1]).path)
        for c in client._session.request.call_args_list
    ]


class TestParseImageChannels(unittest.TestCase):
    def test_nvr_list(self):
        self.assertEqual(
            parse_image_channels(NVR_LIST),
            [
                ImageChannelInfo(1, True, 1),
                ImageChannelInfo(2, True, 1),
                ImageChannelInfo(3, False, None),
            ],
        )

    def test_camera_list_other_namespace(self):
        # A camera names the input videoInputID, an NVR names it inputPort.
        self.assertEqual(parse_image_channels(CAMERA_LIST), [ImageChannelInfo(1, True, 1)])

    def test_real_camera_list_only_channel_ids_are_read(self):
        # The list document of a real camera also carries older, differently
        # named copies of the settings (e.g. WDRExt, WhiteBlance); only the
        # channel entries are read from it.
        xml = (Path(__file__).parent / "fixtures" / "image_channels_list_fixed_basic-DS-2CD2385G1.xml").read_bytes()
        self.assertEqual(parse_image_channels(xml), [ImageChannelInfo(1, True, 1)])

    def test_single_channel_document(self):
        xml = b'<ImageChannel xmlns="x"><id>7</id><enabled>false</enabled></ImageChannel>'
        self.assertEqual(parse_image_channels(xml), [ImageChannelInfo(7, False, None)])

    def test_entries_without_integer_id_are_skipped(self):
        xml = b"<L><ImageChannel><id>abc</id></ImageChannel><ImageChannel><enabled>true</enabled></ImageChannel></L>"
        self.assertEqual(parse_image_channels(xml), [])

    def test_empty_list(self):
        self.assertEqual(parse_image_channels(b"<ImageChannellist/>"), [])

    def test_repeated_id_is_listed_once(self):
        xml = b"<L><ImageChannel><id>1</id><inputPort>3</inputPort></ImageChannel><ImageChannel><id>1</id></ImageChannel></L>"
        self.assertEqual(parse_image_channels(xml), [ImageChannelInfo(1, None, 3)])


class TestGetImageChannels(unittest.TestCase):
    def setUp(self):
        self.client = ISAPIClient(host="192.168.1.100", username="admin", password="pw")
        self.client.request_raw = MagicMock()

    def test_camera_endpoint(self):
        self.client.request_raw.return_value = RawResponse(200, CAMERA_LIST)
        self.assertEqual([c.id for c in self.client.get_image_channels()], [1])
        self.client.request_raw.assert_called_once_with("GET", DIRECT)

    def test_nvr_endpoint(self):
        self.client.request_raw.return_value = RawResponse(200, NVR_LIST)
        self.assertEqual([c.id for c in self.client.get_image_channels(proxy=True)], [1, 2, 3])
        self.client.request_raw.assert_called_once_with("GET", PROXY)

    def test_not_found(self):
        self.client.request_raw.return_value = RawResponse(404, b"")
        with self.assertRaises(ISAPINotFoundError):
            self.client.get_image_channels(proxy=True)

    def test_invalid_xml(self):
        self.client.request_raw.return_value = RawResponse(200, b"junk")
        with self.assertRaises(ISAPIError):
            self.client.get_image_channels()

    def test_channel_listed_but_its_settings_are_not_supported(self):
        # An NVR can list a channel whose camera it cannot proxy image settings
        # for (for example one added over ONVIF). That is reported per channel.
        not_supported = (
            b'<ResponseStatus xmlns="x"><statusCode>4</statusCode>'
            b"<statusString>Invalid Operation</statusString>"
            b"<subStatusCode>notSupport</subStatusCode></ResponseStatus>"
        )
        self.client.request_raw.side_effect = [
            RawResponse(200, NVR_LIST),
            RawResponse(400, not_supported),
            RawResponse(400, not_supported),
        ]
        channels = self.client.get_image_channels(proxy=True)
        with self.assertRaises(ISAPINotSupportedError) as ctx:
            self.client.get_image_settings(channel=channels[2].id, proxy=True)
        self.assertEqual(ctx.exception.sub_status, "notSupport")
        with self.assertRaises(ISAPINotSupportedError):
            self.client.set_image_setting("Gain/GainLevel", "10", channel=3, proxy=True)

    def test_unauthorized(self):
        self.client.request_raw.return_value = RawResponse(401, HTML_ERROR_PAGE)
        with self.assertRaises(ISAPIAuthError) as ctx:
            self.client.get_image_channels(proxy=True)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_nvr_without_image_proxy(self):
        self.client.request_raw.return_value = RawResponse(404, b"")
        with self.assertRaises(ISAPINotSupportedError):
            self.client.get_image_channels(proxy=True)


class TestProxyReadWrite(unittest.TestCase):
    """The same read-modify-write, addressed through the NVR."""

    def setUp(self):
        self.camera = FakeCamera(base=PROXY + "/2")
        self.client = client_for(self.camera)

    def test_settings_read_through_proxy_path(self):
        values = self.client.get_image_values(channel=2, proxy=True)
        self.assertEqual(values["Shutter/ShutterLevel"], "1/1000")
        self.assertEqual(sent(self.client), [("GET", PROXY + "/2")])

    def test_discovery_reads_capabilities_and_values_through_proxy(self):
        original = self.camera.request

        def serve(method, url, **kw):
            if url.endswith("/capabilities"):
                return MagicMock(status_code=200, content=_caps(), headers={})
            return original(method, url, **kw)

        self.client._session.request.side_effect = serve
        settings = {s.path: s for s in self.client.get_image_settings(channel=2, proxy=True)}
        self.assertEqual(settings["WDR/WDRLevel"].value, "41")
        self.assertEqual(
            [p for _, p in sent(self.client)],
            [PROXY + "/2/capabilities", PROXY + "/2"],
        )

    def test_write_goes_to_proxy_path_and_preserves_the_document(self):
        result = self.client.set_image_setting("Shutter/ShutterLevel", "1/500", channel=2, proxy=True)
        self.assertTrue(result.success)
        self.assertEqual([p for p, _ in self.camera.puts], [PROXY + "/2"])
        self.assertEqual(
            self.camera.xml,
            VALUES.replace("<ShutterLevel>1/1000</ShutterLevel>", "<ShutterLevel>1/500</ShutterLevel>"),
        )

    def test_channel_id_may_be_a_string(self):
        self.assertTrue(self.client.set_image_setting("Gain/GainLevel", "50", channel="2", proxy=True).success)

    def test_channel_id_is_one_path_segment(self):
        for proxy, base in ((True, PROXY), (False, "/ISAPI/Image/channels")):
            with self.subTest(proxy=proxy):
                self.assertEqual(
                    self.client._image_endpoint("1/../x", proxy), base + "/1%2F..%2Fx"
                )

    def test_conflicts_are_resolved_on_the_proxy_path(self):
        result = self.client.set_image_setting("WDR/mode", "open", channel=2, proxy=True)
        self.assertTrue(result.success)
        self.assertEqual(get_text(self.camera.xml, "BLC/enabled"), "false")
        self.assertEqual({p for p, _ in self.camera.puts}, {PROXY + "/2"})
        self.assertEqual(len(self.camera.puts), 3)

    def test_no_sub_endpoint_fallback_through_the_proxy(self):
        camera = FakeCamera(base=PROXY + "/2", full_put_supported=False)
        client = client_for(camera)
        result = client.set_image_setting("Shutter/ShutterLevel", "1/500", channel=2, proxy=True)
        self.assertEqual(result.sub_status, "notSupport")
        self.assertEqual(len(camera.puts), 1)
        self.assertEqual([r for r in sent(client) if r[0] == "GET"], [("GET", PROXY + "/2")])

    def test_linked_setting_through_proxy(self):
        from pyhik.image import parse_image_capabilities

        blc = {s.path: s for s in parse_image_capabilities(_caps(), VALUES.encode())}["BLC/BLCMode"]
        result = self.client.set_image_linked_setting(blc, "UP", channel=2, proxy=True)
        self.assertTrue(result.success)
        self.assertEqual(get_text(self.camera.xml, "BLC/BLCMode"), "UP")
        self.assertEqual({p for p, _ in self.camera.puts}, {PROXY + "/2"})

    def test_unauthorized_write_raises(self):
        camera = RejectingCamera({("PUT", PROXY + "/2"): (401, HTML_ERROR_PAGE)}, base=PROXY + "/2")
        with self.assertRaises(ISAPIAuthError) as ctx:
            client_for(camera).set_image_setting("Gain/GainLevel", "10", channel=2, proxy=True)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(camera.xml, VALUES)

    def test_error_page_on_write_is_a_failed_result(self):
        camera = RejectingCamera({("PUT", PROXY + "/2"): (502, HTML_ERROR_PAGE)}, base=PROXY + "/2")
        with self.assertLogs("pyhik.isapi", "WARNING"):
            result = client_for(camera).set_image_setting("Gain/GainLevel", "10", channel=2, proxy=True)
        self.assertEqual((result.success, result.status_code, result.sub_status), (False, 502, NO_SUB_STATUS))

    def test_direct_default_is_unchanged(self):
        camera = FakeCamera()
        self.assertTrue(client_for(camera).set_image_setting("Gain/GainLevel", "50").success)
        self.assertEqual([p for p, _ in camera.puts], [DIRECT + "/1"])


def _caps():
    return (Path(__file__).parent / "fixtures" / "image_capabilities_fixed_basic-DS-2CD2385G1.xml").read_bytes()


if __name__ == "__main__":
    unittest.main()
