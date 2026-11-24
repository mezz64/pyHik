"""Detections of a video channel: on and off."""

from pathlib import Path
import unittest
from unittest.mock import MagicMock
from urllib.parse import urlparse

from pyhik.isapi import (
    DETECTIONS,
    EVENT_ENDPOINTS,
    ISAPIAuthError,
    ISAPIClient,
    ISAPIError,
    ISAPINotSupportedError,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / f"detection_{name}.xml").read_text()


# As read from a camera: the flag below the root is on, the region's is off.
FIELD = fixture("field_detection")
# No namespace on this one.
LINE = fixture("line_detection")
SCENE = fixture("scene_change_detection")
TAMPER = fixture("tamper_detection")
NOT_SUPPORTED = (403, fixture("region_entrance_not_supported"))

FIELD_PATH = "/ISAPI/Smart/FieldDetection/1"


def status(code, sub_status, http=200):
    text = "OK" if http == 200 else "Invalid Operation"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ResponseStatus version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
        f"<requestURL>x</requestURL><statusCode>{code}</statusCode>"
        f"<statusString>{text}</statusString><subStatusCode>{sub_status}</subStatusCode>"
        "</ResponseStatus>"
    )


class FakeCamera:
    """Answers GETs from its documents, and keeps what is PUT."""

    def __init__(self, documents=None):
        self.documents = {
            "/ISAPI/System/Video/inputs/channels/1/motionDetection": (
                200, "<MotionDetection><enabled>true</enabled></MotionDetection>"
            ),
            "/ISAPI/Smart/LineDetection/1": (200, LINE),
            FIELD_PATH: (200, FIELD),
            "/ISAPI/Smart/RegionEntrance/1": NOT_SUPPORTED,
            "/ISAPI/Smart/RegionExiting/1": NOT_SUPPORTED,
            "/ISAPI/System/Video/inputs/channels/1/tamperDetection": (200, TAMPER),
            "/ISAPI/Smart/SceneChangeDetection/1": (200, SCENE),
            "/ISAPI/System/Video/inputs/channels/1/videoLoss": NOT_SUPPORTED,
        }
        self.documents.update(documents or {})
        self.puts = []
        self.put_answer = (200, status(1, "ok"))

    def request(self, method, url, auth=None, params=None, data=None,
                headers=None, timeout=None):
        path = urlparse(url).path
        resp = MagicMock(headers={})
        if method == "GET":
            code, body = self.documents.get(path, (404, ""))
        else:
            self.puts.append((path, data.decode()))
            code, body = self.put_answer
            if code == 200:
                self.documents[path] = (200, data.decode())
        resp.status_code = code
        resp.content = body.encode()
        return resp


def client_for(camera):
    client = ISAPIClient(host="192.168.1.100", username="admin", password="pw")
    client._auth = MagicMock()
    client._session = MagicMock()
    client._session.request.side_effect = camera.request
    return client


class TestDetections(unittest.TestCase):
    def test_all_have_an_endpoint(self):
        for detection in DETECTIONS:
            self.assertIn("{channel}", EVENT_ENDPOINTS[detection])

    def test_video_loss(self):
        self.assertEqual(
            EVENT_ENDPOINTS["videoLoss"],
            "/ISAPI/System/Video/inputs/channels/{channel}/videoLoss",
        )


class TestRead(unittest.TestCase):
    def test_states(self):
        client = client_for(FakeCamera())
        expected = {
            "motionDetection": True,
            "lineDetection": True,
            "fieldDetection": True,
            "tamperDetection": False,
            "sceneChangeDetection": False,
        }
        for detection, enabled in expected.items():
            with self.subTest(detection=detection):
                self.assertIs(client.get_detection_enabled(detection), enabled)

    def test_only_the_flag_below_the_root(self):
        # The region inside is off: the detection is still on.
        self.assertIs(client_for(FakeCamera()).get_detection_enabled("fieldDetection"), True)
        doc = "<X><Region><enabled>true</enabled></Region><enabled>false</enabled></X>"
        camera = FakeCamera({FIELD_PATH: (200, doc)})
        self.assertIs(client_for(camera).get_detection_enabled("fieldDetection"), False)

    def test_not_supported(self):
        answers = [
            NOT_SUPPORTED,
            (403, status(4, "invalidOperation", 403)),
            (404, ""),
            (501, ""),
            (400, status(4, "notSupport", 400)),
        ]
        for answer in answers:
            with self.subTest(answer=answer[0]):
                camera = FakeCamera({FIELD_PATH: answer})
                with self.assertRaises(ISAPINotSupportedError):
                    client_for(camera).get_detection_enabled("fieldDetection")

    def test_rejected_password(self):
        camera = FakeCamera({FIELD_PATH: (401, "<html>Unauthorized</html>")})
        with self.assertRaises(ISAPIAuthError) as ctx:
            client_for(camera).get_detection_enabled("fieldDetection")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_not_permitted(self):
        camera = FakeCamera({FIELD_PATH: (403, status(4, "lowPrivilege", 403))})
        with self.assertRaises(ISAPIAuthError) as ctx:
            client_for(camera).get_detection_enabled("fieldDetection")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_other_errors(self):
        for doc in [(500, status(6, "deviceError", 500)), (200, "<html>login</html"),
                    (200, "<FieldDetection><id>1</id></FieldDetection>")]:
            with self.subTest(doc=doc):
                camera = FakeCamera({FIELD_PATH: doc})
                with self.assertRaises(ISAPIError) as ctx:
                    client_for(camera).get_detection_enabled("fieldDetection")
                self.assertNotIsInstance(ctx.exception, ISAPINotSupportedError)
                self.assertNotIsInstance(ctx.exception, ISAPIAuthError)

    def test_another_channel(self):
        camera = FakeCamera({"/ISAPI/Smart/FieldDetection/2": (200, SCENE)})
        self.assertIs(client_for(camera).get_detection_enabled("fieldDetection", 2), False)

    def test_channel_is_one_path_segment(self):
        camera = FakeCamera({"/ISAPI/Smart/FieldDetection/a%2Fb": (200, FIELD)})
        self.assertIs(client_for(camera).get_detection_enabled("fieldDetection", "a/b"), True)

    def test_unknown_detection(self):
        with self.assertRaises(ValueError):
            client_for(FakeCamera()).get_detection_enabled("nothing")


class TestWrite(unittest.TestCase):
    def test_off(self):
        camera = FakeCamera()
        result = client_for(camera).set_detection_enabled("fieldDetection", False)
        self.assertTrue(result.success)
        (path, body), = camera.puts
        self.assertEqual(path, FIELD_PATH)
        # Only the flag below the root changed: regions and the rest as read.
        self.assertEqual(
            body,
            FIELD.replace(
                "<id>1</id>\n<enabled>true</enabled>", "<id>1</id>\n<enabled>false</enabled>", 1
            ),
        )
        self.assertNotEqual(body, FIELD)

    def test_on(self):
        camera = FakeCamera()
        client = client_for(camera)
        self.assertTrue(client.set_detection_enabled("sceneChangeDetection", True).success)
        self.assertIs(client.get_detection_enabled("sceneChangeDetection"), True)
        (_, body), = camera.puts
        self.assertEqual(body, SCENE.replace("<enabled>false</enabled>", "<enabled>true</enabled>"))

    def test_without_namespace(self):
        camera = FakeCamera()
        client = client_for(camera)
        client.set_detection_enabled("lineDetection", False)
        (_, body), = camera.puts
        # The line item's flag (already off) and the line are kept.
        self.assertEqual(body, LINE.replace("<id>1</id>\n<enabled>true</enabled>",
                                            "<id>1</id>\n<enabled>false</enabled>", 1))

    def test_region_flag_not_touched_when_it_comes_first(self):
        doc = "<X><Region><enabled>false</enabled></Region><enabled>false</enabled></X>"
        camera = FakeCamera({FIELD_PATH: (200, doc)})
        client_for(camera).set_detection_enabled("fieldDetection", True)
        (_, body), = camera.puts
        self.assertEqual(
            body, "<X><Region><enabled>false</enabled></Region><enabled>true</enabled></X>"
        )

    def test_unchanged_sends_nothing(self):
        camera = FakeCamera()
        result = client_for(camera).set_detection_enabled("fieldDetection", True)
        self.assertTrue(result.success)
        self.assertEqual(camera.puts, [])

    def test_refused(self):
        camera = FakeCamera()
        camera.put_answer = (400, status(6, "badParameters", 400))
        result = client_for(camera).set_detection_enabled("fieldDetection", False)
        self.assertFalse(result.success)
        self.assertEqual(result.sub_status, "badParameters")

    def test_rejected_password(self):
        camera = FakeCamera()
        camera.put_answer = (401, "")
        with self.assertRaises(ISAPIAuthError):
            client_for(camera).set_detection_enabled("fieldDetection", False)

    def test_not_supported_sends_nothing(self):
        camera = FakeCamera()
        with self.assertRaises(ISAPINotSupportedError):
            client_for(camera).set_detection_enabled("videoLoss", True)
        self.assertEqual(camera.puts, [])

    def test_without_the_flag(self):
        camera = FakeCamera({FIELD_PATH: (200, "<FieldDetection><id>1</id></FieldDetection>")})
        result = client_for(camera).set_detection_enabled("fieldDetection", True)
        self.assertEqual(result.sub_status, "pathNotFound")
        self.assertEqual(camera.puts, [])


class TestLegacy(unittest.TestCase):
    def client(self, camera):
        client = client_for(camera)
        client.get_cameras = MagicMock(return_value=[MagicMock(id=1)])
        return client

    def test_event_states_leave_out_what_is_not_supported(self):
        states = self.client(FakeCamera()).get_event_states()
        self.assertEqual(
            {state.type: state.enabled for state in states},
            {
                "motionDetection": True,
                "lineDetection": True,
                "fieldDetection": True,
                "tamperDetection": False,
                "sceneChangeDetection": False,
            },
        )

    def test_event_states_stop_on_a_rejected_password(self):
        camera = FakeCamera({FIELD_PATH: (401, "")})
        with self.assertRaises(ISAPIAuthError):
            self.client(camera).get_event_states()

    def test_set_event_enabled(self):
        camera = FakeCamera()
        self.client(camera).set_event_enabled("tamperDetection", 1, True)
        (path, body), = camera.puts
        self.assertTrue(path.endswith("/tamperDetection"))
        self.assertEqual(body, TAMPER.replace("<enabled>false</enabled>", "<enabled>true</enabled>", 1))

    def test_set_event_enabled_refused(self):
        camera = FakeCamera()
        camera.put_answer = (400, status(6, "badParameters", 400))
        with self.assertRaises(ISAPIError) as ctx:
            self.client(camera).set_event_enabled("fieldDetection", 1, False)
        self.assertEqual(ctx.exception.sub_status, "badParameters")

    def test_set_event_enabled_unknown(self):
        with self.assertRaises(ISAPIError):
            self.client(FakeCamera()).set_event_enabled("nothing", 1, True)


if __name__ == "__main__":
    unittest.main()
