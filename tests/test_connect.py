"""Offline tests for choosing the brick from the phone (Connection screen): switching address,
and finding bricks on the PC's networks. No robot, no SSH, no web server.

Run from the project folder:   python -m unittest discover -s tests -v
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("paramiko", types.ModuleType("paramiko"))   # not needed offline
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_phone import ControllerTest, FakeBrick, phone, rc   # noqa: E402  (same fixtures)
import ev3_find   # noqa: E402


class SwitchingBrick(FakeBrick):
    """Records close/connect, and which address each connect() was made to."""

    def __init__(self, log):
        super().__init__()
        self.log = log

    def close(self):
        self.log.append("close")
        self.ctl = None

    def connect(self):
        self.log.append(f"connect {rc.HOST}")
        self.ctl = types.SimpleNamespace(closed=False)

    def read_motors(self):
        return {port: (0, 0) for port in self.paths}


class Enough(Exception):
    """Out of _monitor's forever loop."""


class ConnectTest(ControllerTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(rc, "HOST", "ev3kk.local")   # connect_to changes it: restore after
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller.brick_host = rc.HOST

    def test_address_becomes_host_and_driving_stops(self):
        self.drive(held={"up"})
        self.assertEqual(self.controller.connect_to(" ev3kishan "), "")
        self.assertEqual(rc.HOST, "ev3kishan.local")   # a bare name gets .local
        self.assertIn("stop > L/command", self.brick.sent[-1])
        self.assertFalse(self.controller.telemetry["connected"])
        self.assertEqual(self.controller.connect_to("192.168.4.57"), "")
        self.assertEqual(rc.HOST, "192.168.4.57")        # an IP stays as it is

    def test_bad_addresses_are_refused(self):
        for bad in ("", "   ", "ev3 kishan", "rm -rf /", "a;b", None, 42):
            self.assertTrue(self.controller.connect_to(bad), bad)
        self.assertEqual(rc.HOST, "ev3kk.local")

    def test_remember_saves_this_pcs_brick(self):
        with mock.patch.object(rc.ev3_config, "save_host") as save:
            self.controller.connect_to("192.168.4.57")
            save.assert_not_called()
            self.controller.connect_to("192.168.4.57", remember=True)
            save.assert_called_once_with("192.168.4.57")

    def test_monitor_lets_the_old_brick_go_and_connects_the_new_one(self):
        log = []
        self.controller.brick = SwitchingBrick(log)
        self.controller.connect_to("192.168.4.57")
        rounds = []

        def sleep(seconds):
            rounds.append(seconds)
            if len(rounds) == 2:
                raise Enough

        with mock.patch.object(phone.time, "sleep", sleep), self.assertRaises(Enough):
            self.controller._monitor()
        self.assertEqual(log, ["close", "connect 192.168.4.57"])   # old one stopped first, then the new
        self.assertEqual(self.controller.brick_host, "192.168.4.57")
        self.assertEqual(self.controller.status, "Connected")

    def test_connection_state_for_the_page(self):
        self.brick.name = "ev3kk"
        self.controller.status = "Connected"
        with mock.patch.object(rc.ev3_config, "saved_host", return_value="ev3kk.local"):
            c = self.controller.state()["connection"]
        self.assertEqual((c["host"], c["name"], c["saved"]), ("ev3kk.local", "ev3kk", "ev3kk.local"))
        self.assertFalse(c["scan"]["running"])


class FindBricksTest(unittest.TestCase):
    """ev3_find, shared by the phone and EV3 RC."""

    def setUp(self):
        for patcher in (mock.patch.object(ev3_find, "lan_addresses", return_value=["192.168.4.41", "192.168.0.2"]),
                        mock.patch.object(ev3_find, "default_route_ip", return_value="192.168.4.41")):   # Wi-Fi
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_searches_private_networks_links_first(self):
        with mock.patch.object(ev3_find, "lan_addresses",
                               return_value=["192.168.4.41", "169.254.9.9", "8.8.4.4", "192.168.0.2"]):
            nets = [(str(n), via) for n, via in ev3_find.scan_networks()]
        self.assertEqual(nets, [("192.168.0.0/24", "Bluetooth / USB"),   # the small link first
                                ("192.168.4.0/22", "Wi-Fi")])            # not link-local or public ones

    def test_only_ev3dev_like_ssh_counts_and_nothing_logs_in(self):
        banners = {"192.168.4.57": "SSH-2.0-OpenSSH_7.4p1 Debian-10+deb9u7",   # ev3dev on Wi-Fi
                   "192.168.0.1": "SSH-2.0-OpenSSH_7.4p1 Debian-10+deb9u7",    # ev3dev over Bluetooth
                   "192.168.4.1": "SSH-2.0-dropbear_2020.81",                  # a router
                   "192.168.4.41": "SSH-2.0-OpenSSH_9.0p1 Debian-1"}           # this PC itself
        names = {"192.168.4.57": "ev3krutin.lan", "192.168.0.1": "ev3kk"}
        reports = []
        with mock.patch.object(ev3_find, "ssh_banner", side_effect=lambda ip: banners.get(ip)) as banner,              mock.patch.object(ev3_find.socket, "gethostbyaddr", side_effect=lambda ip: (names[ip], [], [])):
            found = ev3_find.find_bricks(report=lambda so_far: reports.append([dict(f) for f in so_far]))
        self.assertEqual(found, [{"ip": "192.168.0.1", "name": "ev3kk", "via": "Bluetooth / USB"},
                                 {"ip": "192.168.4.57", "name": "ev3krutin", "via": "Wi-Fi"}])
        asked = [call.args[0] for call in banner.call_args_list]
        self.assertEqual(asked[0], "192.168.0.1")                # the link's brick address first
        self.assertEqual(reports[0], [{"ip": "192.168.0.1", "name": None, "via": "Bluetooth / USB"}])   # at once
        self.assertNotIn("192.168.4.41", asked)                  # never probes itself
        self.assertEqual(len(asked), len(set(asked)))            # nothing twice
        self.assertEqual(len(asked), (254 - 1) + (1022 - 1))     # the /24 link and the /22 Wi-Fi, minus the PC

    def test_scan_lists_paired_bricks_without_a_network(self):
        with mock.patch.object(phone.threading, "Thread"):
            controller = phone.Controller()
        found = [{"ip": "192.168.4.46", "name": "ev3krutin", "via": "Wi-Fi"}]
        with mock.patch.object(ev3_find, "find_bricks", return_value=found),              mock.patch.object(ev3_find, "paired_bluetooth_bricks", return_value=["ev3kk", "ev3krutin"]):
            controller.search.run()
        self.assertEqual(controller.search.state, {"running": False, "found": found, "paired": ["ev3kk"], "error": ""})

    def test_scan_runs_in_the_background_once(self):
        with mock.patch.object(phone.threading, "Thread"):
            controller = phone.Controller()
        with mock.patch.object(ev3_find.threading, "Thread") as thread:
            controller.start_scan()
            controller.start_scan()   # already running: not started twice
        self.assertEqual(thread.call_count, 1)
        with mock.patch.object(ev3_find, "find_bricks", return_value=[]),              mock.patch.object(ev3_find, "paired_bluetooth_bricks", return_value=[]):
            controller.search.run()
        self.assertEqual(controller.search.state, {"running": False, "found": [], "paired": [],
                                           "error": "No bricks found on this PC's networks."})


class AddressTest(unittest.TestCase):
    def test_parse_address(self):
        self.assertEqual(rc.ev3_config.parse_address(" ev3kishan "), "ev3kishan.local")
        self.assertEqual(rc.ev3_config.parse_address("192.168.4.46"), "192.168.4.46")
        for bad in ("", "  ", "ev3 kishan", "a;b", "rm -rf /", None, 42):
            self.assertIsNone(rc.ev3_config.parse_address(bad), bad)


if __name__ == "__main__":
    unittest.main()
