"""Alarm inputs and outputs, and holiday mode."""

import unittest
from unittest.mock import MagicMock
from urllib.parse import urlparse

from pyhik.isapi import (
    ISAPIAuthError,
    ISAPIClient,
    ISAPIError,
    ISAPINotSupportedError,
)

NS = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'

OUTPUTS = f"""<?xml version="1.0" encoding="UTF-8"?>
<IOOutputPortList version="2.0" {NS}>
<IOOutputPort version="2.0" {NS}>
<id>1</id>
<PowerOnState><defaultState>low</defaultState><outputState>pulse</outputState>
<pulseDuration>1000</pulseDuration></PowerOnState>
<name>Siren</name>
</IOOutputPort>
<IOOutputPort version="2.0" {NS}>
<id>2</id>
<PowerOnState><defaultState>low</defaultState><outputState>high</outputState>
</PowerOnState>
</IOOutputPort>
</IOOutputPortList>"""

INPUTS = f"""<?xml version="1.0" encoding="UTF-8"?>
<IOInputPortList version="2.0" {NS}>
<IOInputPort version="2.0" {NS}>
<id>1</id>
<enabled>true</enabled>
<triggering>low</triggering>
<name>Door</name>
</IOInputPort>
</IOInputPortList>"""

INPUT_1 = f"""<?xml version="1.0" encoding="UTF-8"?>
<IOInputPort version="2.0" {NS}>
<id>1</id>
<enabled>true</enabled>
<triggering>low</triggering>
<name>Door</name>
</IOInputPort>"""


def output_status(state):
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<IOPortStatus version="2.0" {NS}>'
        f"<ioPortID>1</ioPortID><ioPortType>output</ioPortType>"
        f"<ioState>{state}</ioState></IOPortStatus>"
    )


# The second holiday is on, the first (holiday mode) is off.
HOLIDAYS = f"""<?xml version="1.0" encoding="UTF-8"?>
<HolidayList version="2.0" {NS}>
<holiday>
<id>1</id>
<enabled>false</enabled>
<holidayName>Holiday1</holidayName>
<holidayMode>date</holidayMode>
<holidayDate><startDate>2026-12-24</startDate><endDate>2026-12-26</endDate></holidayDate>
</holiday>
<holiday>
<id>2</id>
<enabled>true</enabled>
<holidayName>Holiday2</holidayName>
</holiday>
</HolidayList>"""


def status(code, sub_status, http=200):
    text = "OK" if http == 200 else "Invalid Operation"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ResponseStatus version="2.0" {NS}>'
        f"<requestURL>x</requestURL><statusCode>{code}</statusCode>"
        f"<statusString>{text}</statusString><subStatusCode>{sub_status}</subStatusCode>"
        "</ResponseStatus>"
    )


NOT_SUPPORTED = (403, status(4, "notSupport", 403))
OUT = "/ISAPI/System/IO/outputs"
IN = "/ISAPI/System/IO/inputs"
HOL = "/ISAPI/System/Holidays"


class FakeDevice:
    """Answers GETs from its documents, and keeps what is PUT."""

    def __init__(self, documents=None):
        self.documents = {
            OUT: (200, OUTPUTS),
            f"{OUT}/1/status": (200, output_status("inactive")),
            f"{OUT}/2/status": (200, output_status("active")),
            IN: (200, INPUTS),
            f"{IN}/1": (200, INPUT_1),
            HOL: (200, HOLIDAYS),
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
            if code == 200 and not path.endswith("/trigger"):
                self.documents[path] = (200, data.decode())
        resp.status_code = code
        resp.content = body.encode()
        return resp


def client_for(device):
    client = ISAPIClient(host="192.168.1.100", username="admin", password="pw")
    client._auth = MagicMock()
    client._session = MagicMock()
    client._session.request.side_effect = device.request
    return client


class TestPorts(unittest.TestCase):
    def test_outputs(self):
        ports = client_for(FakeDevice()).get_output_ports()
        self.assertEqual([(p.id, p.name) for p in ports], [("1", "Siren"), ("2", "Output 2")])

    def test_a_port_without_an_id_is_left_out(self):
        doc = f"<IOOutputPortList {NS}><IOOutputPort><name>x</name></IOOutputPort></IOOutputPortList>"
        self.assertEqual(client_for(FakeDevice({OUT: (200, doc)})).get_output_ports(), [])

    def test_inputs(self):
        ports = client_for(FakeDevice()).get_input_ports()
        self.assertEqual([(p.id, p.name) for p in ports], [("1", "Door")])

    def test_none(self):
        for answer in (NOT_SUPPORTED, (404, ""), (403, status(4, "invalidOperation", 403))):
            with self.subTest(answer=answer[0]):
                client = client_for(FakeDevice({OUT: answer, IN: answer}))
                self.assertEqual(client.get_output_ports(), [])
                self.assertEqual(client.get_input_ports(), [])

    def test_rejected_password(self):
        with self.assertRaises(ISAPIAuthError) as ctx:
            client_for(FakeDevice({OUT: (401, "")})).get_output_ports()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_other_error(self):
        with self.assertRaises(ISAPIError) as ctx:
            client_for(FakeDevice({IN: (500, status(6, "deviceError", 500))})).get_input_ports()
        self.assertNotIsInstance(ctx.exception, ISAPINotSupportedError)


class TestOutputs(unittest.TestCase):
    def test_state(self):
        client = client_for(FakeDevice())
        self.assertIs(client.get_output_active("1"), False)
        self.assertIs(client.get_output_active("2"), True)

    def test_set(self):
        device = FakeDevice()
        result = client_for(device).set_output_active("1", True)
        self.assertTrue(result.success)
        (path, body), = device.puts
        self.assertEqual(path, f"{OUT}/1/trigger")
        self.assertIn("<outputState>high</outputState>", body)
        client_for(device).set_output_active("1", False)
        self.assertIn("<outputState>low</outputState>", device.puts[-1][1])

    def test_refused(self):
        device = FakeDevice()
        device.put_answer = (403, status(4, "invalidOperation", 403))
        result = client_for(device).set_output_active("1", True)
        self.assertFalse(result.success)
        self.assertEqual(result.sub_status, "invalidOperation")

    def test_rejected_password(self):
        device = FakeDevice()
        device.put_answer = (401, "")
        with self.assertRaises(ISAPIAuthError):
            client_for(device).set_output_active("1", True)
        with self.assertRaises(ISAPIAuthError):
            client_for(FakeDevice({f"{OUT}/1/status": (401, "")})).get_output_active("1")

    def test_not_supported(self):
        with self.assertRaises(ISAPINotSupportedError):
            client_for(FakeDevice()).get_output_active("9")

    def test_status_without_state(self):
        device = FakeDevice({f"{OUT}/1/status": (200, "<IOPortStatus><ioPortID>1</ioPortID></IOPortStatus>")})
        with self.assertRaises(ISAPIError):
            client_for(device).get_output_active("1")

    def test_id_is_one_path_segment(self):
        device = FakeDevice({f"{OUT}/a%2Fb/status": (200, output_status("active"))})
        self.assertIs(client_for(device).get_output_active("a/b"), True)

    def test_legacy(self):
        device = FakeDevice()
        client = client_for(device)
        self.assertIs(client.get_output_state("2"), True)
        # Unreadable: false, as before.
        self.assertIs(client.get_output_state("9"), False)
        client.set_output_state("1", True)
        self.assertIn("<outputState>high</outputState>", device.puts[-1][1])
        device.put_answer = (400, status(6, "badParameters", 400))
        with self.assertRaises(ISAPIError):
            client.set_output_state("1", False)

    def test_legacy_rejected_password(self):
        client = client_for(FakeDevice({f"{OUT}/1/status": (401, "")}))
        with self.assertRaises(ISAPIAuthError):
            client.get_output_state("1")


class TestInputs(unittest.TestCase):
    def test_enabled(self):
        self.assertIs(client_for(FakeDevice()).get_input_enabled("1"), True)

    def test_disable_keeps_the_rest(self):
        device = FakeDevice()
        result = client_for(device).set_input_enabled("1", False)
        self.assertTrue(result.success)
        (path, body), = device.puts
        self.assertEqual(path, f"{IN}/1")
        self.assertEqual(body, INPUT_1.replace("<enabled>true</enabled>", "<enabled>false</enabled>"))

    def test_unchanged_sends_nothing(self):
        device = FakeDevice()
        self.assertTrue(client_for(device).set_input_enabled("1", True).success)
        self.assertEqual(device.puts, [])

    def test_not_supported(self):
        with self.assertRaises(ISAPINotSupportedError):
            client_for(FakeDevice({f"{IN}/1": NOT_SUPPORTED})).get_input_enabled("1")


PROXY_INPUTS = f"""<?xml version="1.0" encoding="UTF-8"?>
<IOProxyInputPortList version="2.0" {NS}>
<IOProxyInputPort>
<id>201</id>
<enabled>false</enabled>
<IODescriptor><userName>admin</userName><password>secret</password>
<addressingFormatType>ipaddress</addressingFormatType><ipAddress>10.0.0.2</ipAddress>
<managePortNo>8000</managePortNo><innerIOPortID>1</innerIOPortID></IODescriptor>
<triggering>high</triggering>
<name>Gate</name>
</IOProxyInputPort>
</IOProxyInputPortList>"""

PROXY_OUTPUTS = f"""<?xml version="1.0" encoding="UTF-8"?>
<IOProxyOutputPortList version="2.0" {NS}>
<IOProxyOutputPort><id>202</id><name>Light</name></IOProxyOutputPort>
</IOProxyOutputPortList>"""

STATUS = f"""<?xml version="1.0" encoding="UTF-8"?>
<IOPortStatusList version="2.0" {NS}>
<IOPortStatus><ioPortID>1</ioPortID><ioPortType>input</ioPortType><ioState>active</ioState></IOPortStatus>
<IOPortStatus><ioPortID>1</ioPortID><ioPortType>output</ioPortType><ioState>inactive</ioState></IOPortStatus>
<IOPortStatus><ioPortType>output</ioPortType><ioState>active</ioState></IOPortStatus>
</IOPortStatusList>"""

ONE_HOLIDAY = f"""<?xml version="1.0" encoding="UTF-8"?>
<holiday version="2.0" {NS}>
<id>2</id>
<enabled>true</enabled>
<holidayName>Holiday2</holidayName>
</holiday>"""


class TestProxyPortsAndStatus(unittest.TestCase):
    def device(self):
        return FakeDevice({
            "/ISAPI/ContentMgmt/IOProxy/inputs": (200, PROXY_INPUTS),
            "/ISAPI/ContentMgmt/IOProxy/outputs": (200, PROXY_OUTPUTS),
            "/ISAPI/System/IO/status": (200, STATUS),
            "/ISAPI/ContentMgmt/IOProxy/status": (200, STATUS.replace("<ioPortID>1<", "<ioPortID>201<")),
            f"{IN}/1/status": (200, output_status("active").replace("output", "input")),
        })

    def test_input_list_gives_enabled(self):
        client = client_for(self.device())
        self.assertEqual(client.get_input_ports()[0].enabled, True)
        doc = f"<IOInputPortList {NS}><IOInputPort><id>1</id></IOInputPort></IOInputPortList>"
        self.assertIsNone(client_for(FakeDevice({IN: (200, doc)})).get_input_ports()[0].enabled)

    def test_proxy_ports(self):
        client = client_for(self.device())
        [port] = client.get_input_ports(proxy=True)
        self.assertEqual((port.id, port.name, port.enabled), ("201", "Gate", False))
        self.assertEqual(
            [(p.id, p.name) for p in client.get_output_ports(proxy=True)], [("202", "Light")]
        )

    def test_no_proxy_ports(self):
        client = client_for(FakeDevice())
        self.assertEqual(client.get_input_ports(proxy=True), [])
        self.assertEqual(client.get_output_ports(proxy=True), [])

    def test_status(self):
        states = client_for(self.device()).get_io_status()
        self.assertEqual(
            [(s.id, s.type, s.active) for s in states],
            [("1", "input", True), ("1", "output", False)],
        )

    def test_proxy_status(self):
        states = client_for(self.device()).get_io_status(proxy=True)
        self.assertEqual(states[0].id, "201")

    def test_status_not_supported(self):
        with self.assertRaises(ISAPINotSupportedError):
            client_for(FakeDevice()).get_io_status()

    def test_status_rejected_password(self):
        device = FakeDevice({"/ISAPI/System/IO/status": (401, "")})
        with self.assertRaises(ISAPIAuthError):
            client_for(device).get_io_status()

    def test_input_active(self):
        self.assertIs(client_for(self.device()).get_input_active("1"), True)
        with self.assertRaises(ISAPINotSupportedError):
            client_for(FakeDevice()).get_input_active("1")


class TestHoliday(unittest.TestCase):
    def test_list(self):
        holidays = client_for(FakeDevice()).get_holidays()
        self.assertEqual(
            [(h.id, h.name, h.enabled, h.mode, h.start, h.end) for h in holidays],
            [
                ("1", "Holiday1", False, "date", "2026-12-24", "2026-12-26"),
                ("2", "Holiday2", True, "", None, None),
            ],
        )

    def test_none(self):
        client = client_for(FakeDevice({HOL: NOT_SUPPORTED}))
        self.assertEqual(client.get_holidays(), [])
        with self.assertRaises(ISAPIAuthError):
            client_for(FakeDevice({HOL: (401, "")})).get_holidays()

    def test_enable_one_through_its_own_endpoint(self):
        device = FakeDevice({f"{HOL}/2": (200, ONE_HOLIDAY)})
        result = client_for(device).set_holiday_enabled("2", False)
        self.assertTrue(result.success)
        (path, body), = device.puts
        self.assertEqual(path, f"{HOL}/2")
        self.assertEqual(body, ONE_HOLIDAY.replace("<enabled>true</enabled>", "<enabled>false</enabled>"))

    def test_enable_one_in_the_list(self):
        # No endpoint of its own: the list, with only its flag changed.
        device = FakeDevice()
        client = client_for(device)
        self.assertTrue(client.set_holiday_enabled("2", False).success)
        (path, body), = device.puts
        self.assertEqual(path, HOL)
        self.assertEqual(
            body,
            HOLIDAYS.replace("<id>2</id>\n<enabled>true</enabled>", "<id>2</id>\n<enabled>false</enabled>"),
        )
        self.assertEqual([h.enabled for h in client.get_holidays()], [False, False])

    def test_unchanged_sends_nothing(self):
        device = FakeDevice()
        self.assertTrue(client_for(device).set_holiday_enabled("2", True).success)
        self.assertEqual(device.puts, [])

    def test_unknown_holiday(self):
        device = FakeDevice()
        self.assertEqual(client_for(device).set_holiday_enabled("9", True).sub_status, "pathNotFound")
        self.assertEqual(device.puts, [])

    def test_holiday_without_its_flag(self):
        doc = f"<HolidayList {NS}><holiday><id>1</id><holidayName>x</holidayName></holiday></HolidayList>"
        device = FakeDevice({HOL: (200, doc)})
        self.assertEqual(client_for(device).set_holiday_enabled("1", True).sub_status, "pathNotFound")

    def test_no_holidays(self):
        with self.assertRaises(ISAPINotSupportedError):
            client_for(FakeDevice({HOL: NOT_SUPPORTED})).set_holiday_enabled("1", True)

    def test_rejected_password(self):
        device = FakeDevice()
        device.put_answer = (401, "")
        with self.assertRaises(ISAPIAuthError):
            client_for(device).set_holiday_enabled("1", True)

    def test_legacy(self):
        device = FakeDevice()
        client = client_for(device)
        # Any holiday on, as before.
        self.assertIs(client.get_holiday_mode_enabled(), True)
        # The first holiday.
        client.set_holiday_mode_enabled(True)
        (path, body), = device.puts
        self.assertIn("<id>1</id>\n<enabled>true</enabled>", body)
        self.assertIs(client_for(FakeDevice({HOL: NOT_SUPPORTED})).get_holiday_mode_enabled(), False)
        # No holidays: nothing to do, as before.
        empty = FakeDevice({HOL: (200, f"<HolidayList {NS}></HolidayList>")})
        client_for(empty).set_holiday_mode_enabled(True)
        self.assertEqual(empty.puts, [])
        device.put_answer = (400, status(6, "badParameters", 400))
        with self.assertRaises(ISAPIError):
            client.set_holiday_mode_enabled(False)

    def test_legacy_rejected_password(self):
        with self.assertRaises(ISAPIAuthError):
            client_for(FakeDevice({HOL: (401, "")})).get_holiday_mode_enabled()


if __name__ == "__main__":
    unittest.main()
