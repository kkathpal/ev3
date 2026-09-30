"""Offline tests for choosing the brick from the phone (Connection screen): each phone picks its
own brick, and finding bricks on the PC's networks. No robot, no SSH, no web server.

Run from the project folder:   python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.modules.setdefault("paramiko", types.ModuleType("paramiko"))   # not needed offline
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_phone import FakeBrick, phone, rc, speeds   # noqa: E402  (same fixtures)
import ev3_find   # noqa: E402


class HubTest(unittest.TestCase):
    """Each phone picks its own brick; phones on the same brick share it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.saved_hosts = []
        for patcher in (mock.patch.object(phone.threading, "Thread"),              # no brick threads
                        mock.patch.object(rc, "HOST", "ev3kk.local"),              # this PC's brick
                        mock.patch.object(rc, "SETTINGS_FILE", os.path.join(tmp.name, "settings.json")),
                        mock.patch.object(rc.ev3_config, "save_host", self.saved_hosts.append),
                        mock.patch.object(rc.ev3_config, "saved_host", return_value="ev3kk.local")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hub = phone.Hub()

    def test_each_phone_drives_its_own_brick(self):
        self.assertEqual(self.hub.select("A", "ev3kishan"), "")
        self.assertEqual(self.hub.select("B", "192.168.4.46"), "")
        a, b = self.hub.controller("A"), self.hub.controller("B")
        self.assertEqual((a.host, b.host), ("ev3kishan.local", "192.168.4.46"))
        self.assertIsNot(a, b)
        self.assertEqual(self.hub.controller("C").host, "ev3kk.local")   # a new phone: this PC's brick

    def test_picking_a_brick_leaves_other_phones_alone(self):
        self.hub.controller("A"), self.hub.controller("B")   # both start on this PC's brick
        self.hub.select("A", "192.168.4.46")
        self.assertEqual(self.hub.controller("B").host, "ev3kk.local")
        self.assertEqual(self.hub.state("B")["connection"]["host"], "ev3kk.local")
        self.assertEqual(self.hub.state("A")["connection"]["host"], "192.168.4.46")

    def test_bad_addresses_are_refused(self):
        for bad in ("", "   ", "ev3 kishan", "rm -rf /", "a;b", None, 42):
            self.assertTrue(self.hub.select("A", bad), bad)
        self.assertEqual(self.hub.controller("A").host, "ev3kk.local")

    def test_remember_changes_where_new_phones_start(self):
        self.hub.select("A", "192.168.4.46")
        self.assertEqual(self.saved_hosts, [])
        self.hub.select("A", "ev3kishan", remember=True)
        self.assertEqual(self.saved_hosts, ["ev3kishan.local"])
        self.assertEqual(self.hub.controller("new phone").host, "ev3kishan.local")

    def test_moving_on_stops_what_this_phone_was_driving(self):
        home = self.hub.controller("A")
        home.brick = FakeBrick()
        home.drive(["up"], client="A")
        self.hub.select("A", "192.168.4.46")
        self.assertIsNone(speeds(home.brick.sent[-1]))   # the last command was a stop

    def test_unused_bricks_are_let_go(self):
        self.hub.controller("A")                         # the page opens on this PC's brick
        self.hub.select("A", "192.168.4.46")
        self.hub.select("B", "192.168.4.46")
        wifi = self.hub.controllers["192.168.4.46"]
        with mock.patch.object(wifi, "shutdown") as shutdown:
            self.hub.select("A", "ev3kishan")            # B still on it: kept
            shutdown.assert_not_called()
            self.hub.select("B", "ev3kishan")            # nobody left: stopped and let go
            shutdown.assert_called_once()
        self.assertNotIn("192.168.4.46", self.hub.controllers)
        kishan = self.hub.controllers["ev3kishan.local"]
        with mock.patch.object(kishan, "shutdown") as shutdown, self.hub.lock:
            self.hub._release_unused(now=phone.time.monotonic() + phone.REAP_AFTER + 1)   # phones gone
            shutdown.assert_called_once()
        self.assertEqual(list(self.hub.controllers), ["ev3kk.local"])   # this PC's brick is kept

    def test_each_brick_keeps_its_own_settings(self):
        rc.save_settings({"left": "outA", "right": "outD", "invert_left": True, "mode": "Normal"})
        home, other = phone.Controller("ev3kk.local"), phone.Controller("192.168.4.46")
        other.brick.name = "ev3krutin"
        with other.lock:
            other._save({"left": "outB", "right": "outC", "invert_left": False})
        with home.lock:
            home._save({"trim": 5})
        file = rc.load_settings()
        self.assertEqual((file["left"], file["right"], file["trim"]), ("outA", "outD", 5))   # this PC's brick
        self.assertEqual(file["bricks"]["ev3krutin"], {"left": "outB", "right": "outC", "invert_left": False})
        other._reload_settings()
        self.assertEqual((other.settings["left"], other.settings["mode"]), ("outB", "Normal"))   # own + PC's rest
        rc.save_settings({"left": "outA", "right": "outD"})   # the desktop app saving its settings
        self.assertIn("ev3krutin", rc.load_settings()["bricks"])   # doesn't wipe other bricks'

    def test_state_has_the_search_and_the_saved_brick(self):
        c = self.hub.state("A")["connection"]
        self.assertEqual((c["host"], c["saved"]), ("ev3kk.local", "ev3kk.local"))
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

    def test_only_confirmed_ev3_bricks_are_listed(self):
        banners = {"192.168.4.57": "SSH-2.0-OpenSSH_7.4p1 Debian-10+deb9u7",   # ev3krutin on Wi-Fi
                   "192.168.0.1": "SSH-2.0-OpenSSH_7.4p1 Debian-10+deb9u7",    # ev3kk over Bluetooth
                   "192.168.4.60": "SSH-2.0-OpenSSH_8.4p1 Raspbian-5",         # a Raspberry Pi: login fails
                   "192.168.4.1": "SSH-2.0-dropbear_2020.81",                  # a router: not even tried
                   "192.168.4.41": "SSH-2.0-OpenSSH_9.0p1 Debian-1"}           # this PC itself
        bricks = {"192.168.4.57": {"name": "ev3krutin", "battery": 7.2, "motors": ["outB", "outC"]},
                  "192.168.0.1": {"name": "ev3kk", "battery": 7.7, "motors": ["outA", "outD"]}}
        logins, reports = [], []

        def identify(ip, user, password):
            logins.append((ip, user, password))
            return bricks.get(ip)

        with mock.patch.object(ev3_find, "ssh_banner", side_effect=lambda ip: banners.get(ip)) as banner,              mock.patch.object(ev3_find, "identify", side_effect=identify):
            found = ev3_find.find_bricks(report=lambda so_far: reports.append([dict(f) for f in so_far]),
                                         user="robot", password="maker")
        self.assertEqual(found, [{"ip": "192.168.0.1", "via": "Bluetooth / USB", "name": "ev3kk", "battery": 7.7,
                                  "motors": ["outA", "outD"]},
                                 {"ip": "192.168.4.57", "via": "Wi-Fi", "name": "ev3krutin", "battery": 7.2,
                                  "motors": ["outB", "outC"]}])
        self.assertEqual(reports[0][0]["name"], "ev3kk")          # the Bluetooth brick is reported first
        self.assertEqual(sorted(ip for ip, _, _ in logins), ["192.168.0.1", "192.168.4.57", "192.168.4.60"])
        self.assertTrue(all((u, p) == ("robot", "maker") for _, u, p in logins))   # the brick login
        asked = [call.args[0] for call in banner.call_args_list]
        self.assertEqual(asked[0], "192.168.0.1")                # the link's brick address first
        self.assertNotIn("192.168.4.41", asked)                  # never probes itself
        self.assertEqual(len(asked), len(set(asked)))            # nothing twice

    def test_identify_reads_an_ev3_and_rejects_the_rest(self):
        outputs = {"ev3": "battery 7912000\nname ev3krutin\nmotor outB\nmotor outC\n", "pi": "not-ev3\n"}

        class Client:
            def __init__(self, kind): self.kind = kind
            def set_missing_host_key_policy(self, policy): pass
            def connect(self, *a, **k):
                if self.kind == "refused":
                    raise OSError("Authentication failed")
            def exec_command(self, cmd, timeout=None):
                return None, types.SimpleNamespace(read=lambda: outputs[self.kind].encode()), None
            def close(self): pass

        for kind, expected in (("ev3", {"name": "ev3krutin", "battery": 7.9, "motors": ["outB", "outC"]}),
                               ("pi", None), ("refused", None)):
            fake = types.SimpleNamespace(SSHClient=lambda k=kind: Client(k), AutoAddPolicy=lambda: None)
            with mock.patch.dict(sys.modules, {"paramiko": fake}):
                self.assertEqual(ev3_find.identify("192.168.4.57", "robot", "maker"), expected, kind)

    def test_scan_lists_paired_bricks_without_a_network(self):
        search = ev3_find.BrickSearch(("robot", "maker"))
        found = [{"ip": "192.168.4.46", "name": "ev3krutin", "via": "Wi-Fi"}]
        with mock.patch.object(ev3_find, "find_bricks", return_value=found), \
             mock.patch.object(ev3_find, "paired_bluetooth_bricks", return_value=["ev3kk", "ev3krutin"]):
            search.run()
        self.assertEqual(search.state, {"running": False, "found": found, "paired": ["ev3kk"], "error": ""})

    def test_scan_runs_in_the_background_once(self):
        search = ev3_find.BrickSearch(("robot", "maker"))
        with mock.patch.object(ev3_find.threading, "Thread") as thread:
            self.assertTrue(search.start())
            self.assertFalse(search.start())   # already running: not started twice
        self.assertEqual(thread.call_count, 1)
        with mock.patch.object(ev3_find, "find_bricks", return_value=[]), \
             mock.patch.object(ev3_find, "paired_bluetooth_bricks", return_value=[]):
            search.run()
        state = search.state
        self.assertEqual((state["running"], state["found"], state["paired"]), (False, [], []))
        self.assertTrue(state["error"].startswith("No bricks found."), state["error"])


class AddressTest(unittest.TestCase):
    def test_parse_address(self):
        self.assertEqual(rc.ev3_config.parse_address(" ev3kishan "), "ev3kishan.local")
        self.assertEqual(rc.ev3_config.parse_address("192.168.4.46"), "192.168.4.46")
        for bad in ("", "  ", "ev3 kishan", "a;b", "rm -rf /", None, 42):
            self.assertIsNone(rc.ev3_config.parse_address(bad), bad)


if __name__ == "__main__":
    unittest.main()
