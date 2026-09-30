"""Offline tests for the phone controller in ev3_phone.py: no robot, no SSH, no web server.

Run from the project folder:   python -m unittest discover -s tests -v
"""
import os
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.modules.setdefault("paramiko", types.ModuleType("paramiko"))   # not needed offline
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
with mock.patch.object(sys, "argv", ["ev3_phone.py"]):   # no brick address, no --port
    import ev3_phone as phone

rc = phone.rc
TICK = rc.RENEW_MS / 1000
NAN, INF = float("nan"), float("inf")


class FakeBrick(rc.Brick):
    """A connected Brick that records the shell commands it would send instead of sending them."""

    def __init__(self):
        super().__init__()
        self.paths = {"outA": "L", "outD": "R"}
        self.ctl = types.SimpleNamespace(closed=False)   # what `connected` looks at
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)
        return True


class MovedBrick(FakeBrick):
    """A brick whose motor cables get moved from outA/outD to outB/outC after its first read:
    the next read notices (MotorsChanged) and the connect() after that finds the new sockets."""

    def __init__(self, note):
        super().__init__()
        self.note = note   # called with each event, so the test sees the controller's state at it
        self.reads = 0

    def read_motors(self):
        self.reads += 1
        if self.reads == 2:
            self.note("motors changed")
            raise rc.MotorsChanged("motors changed, reconnecting")
        self.note("read")
        return {port: (0, 0) for port in self.paths}

    def drop(self):
        self.note("drop")
        super().drop()   # nothing to close here; leaves it disconnected, as for real

    def connect(self):
        self.note("connect")
        self.paths = {"outB": "L2", "outC": "R2"}
        self.ctl = types.SimpleNamespace(closed=False)


class SettingsFile:
    """ev3_drive_settings.json kept in memory: what the controller reads back after a save,
    without ever touching the user's real calibration."""

    def __init__(self, **settings):
        self.data = settings
        self.saves = []   # every settings dict written, in order

    def save(self, settings):
        self.data = dict(settings)
        self.saves.append(dict(settings))

    def load(self, controller):
        controller.settings = dict(self.data)


def speeds(cmd, paths=("L", "R")):
    """The speed_sp of a drive command for the motors at `paths`, or None if it isn't one (a stop)."""
    found = {path: int(v) for v, path in re.findall(r"echo (-?\d+) > (\w+)/speed_sp", cmd)}
    return tuple(found[p] for p in paths) if all(p in found for p in paths) else None


class ControllerTest(unittest.TestCase):
    """A Controller on a fake brick and a fake settings file, without its monitor and safety threads."""

    def setUp(self):
        self.now = 1000.0
        self.file = SettingsFile(left="outA", right="outD")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(rc.time, "monotonic", lambda: self.now),
                        mock.patch.object(rc, "SETTINGS_FILE", os.path.join(tmp.name, "settings.json")),   # never the user's file,
                        mock.patch.object(rc, "save_settings", self.file.save),                              # which set_mode writes...
                        mock.patch.object(phone.Controller, "_reload_settings", lambda controller: self.file.load(controller))):   # ...and reads
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(phone.threading, "Thread"):   # no monitor / safety threads
            self.controller = phone.Controller()
        self.controller.brick = self.brick = FakeBrick()

    def drive(self, held=(), stick=None, mode=None, ticks=1, paths=("L", "R")):
        """`ticks` phone heartbeats; return the (left, right) speed_sp of the last command sent."""
        for _ in range(ticks):
            self.controller.drive(list(held), mode, stick)
            self.now += TICK
        return speeds(self.brick.sent[-1], paths) if self.brick.sent else None

    def full_ramp(self):
        return int(rc.RAMP_SECONDS / TICK) + 2


class PhoneTest(ControllerTest):
    def test_parse_stick(self):
        self.assertEqual(phone.parse_stick([0.5, -0.25]), (0.5, -0.25))
        self.assertEqual(phone.parse_stick((0, 1)), (0.0, 1.0))
        self.assertEqual(phone.parse_stick([3, -7.5]), (1.0, -1.0))   # clamped to the rim
        for junk in (None, "0,1", 0.5, {"x": 0, "y": 1}, [], [1], [0, 1, 0], [True, 0], [0, False],
                     ["0", 1], [None, 1], [NAN, 1], [1, NAN], [INF, 0], [0, -INF]):
            with self.subTest(value=junk):
                self.assertIsNone(phone.parse_stick(junk))

    def test_stick_forward_drives_both_wheels_gently(self):
        first = self.drive(stick=[0, 1])
        self.assertIn("run-timed", self.brick.sent[-1])
        self.assertEqual(first[0], first[1])
        self.assertGreater(first[0], 0)
        gear = rc.wheel_commands({"up"}, "Normal")[0]
        self.assertLess(first[0], gear)                                        # ramped, not straight to full
        self.assertLess(first[0], rc.MOTOR_LIMIT * TICK / rc.RAMP_SECONDS + 1)   # one renewal's share
        self.assertEqual(self.drive(stick=[0, 1], ticks=self.full_ramp()), (gear, gear))
        self.assertTrue(self.controller.moving)

    def test_gear_from_the_phone_applies_to_the_stick(self):
        slow = rc.wheel_commands({"up"}, "Slow")[0]
        self.assertEqual(self.drive(stick=[0, 1], mode="Slow", ticks=self.full_ramp()), (slow, slow))
        self.assertEqual(self.controller.mode, "Slow")

    def test_dead_zone_while_stopped_sends_nothing(self):
        for stick in ([0, 0], [0.05, -0.05], [rc.STICK_DEADZONE, 0]):
            self.drive(stick=stick, ticks=5)
        self.assertEqual(self.brick.sent, [])   # no stop spam on every heartbeat
        self.assertFalse(self.controller.moving)
        self.assertIsNotNone(self.controller.stick)   # the thumb is still on the stick, though

    def test_stick_back_in_the_dead_zone_stops(self):
        self.drive(stick=[0, 1], ticks=3)
        self.drive(stick=[0.03, 0.02])   # thumb drifted back to the centre
        self.assertIn(f"echo {rc.RELEASE_STOP} > L/stop_action", self.brick.sent[-1])
        self.assertIn("echo stop > R/command", self.brick.sent[-1])
        self.assertFalse(self.controller.moving)
        sent = len(self.brick.sent)
        self.drive(stick=[0.03, 0.02], ticks=5)
        self.assertEqual(len(self.brick.sent), sent)   # stopped once, then quiet

    def test_no_input_stops(self):
        self.drive(stick=[0, 1], ticks=3)
        self.drive()   # held=[] and no stick: nothing pressed on the page
        self.assertIn(f"echo {rc.RELEASE_STOP} > L/stop_action", self.brick.sent[-1])
        self.assertFalse(self.controller.moving)
        self.assertIsNone(self.controller.stick)
        self.assertEqual(self.controller.held, set())

    def test_invalid_stick_never_drives(self):
        for stick in ([NAN, 1], [1, INF], [True, 1], "0,1", [0, 1, 0]):
            with self.subTest(stick=stick):
                self.drive(stick=stick, ticks=3)
                self.assertIsNone(self.controller.stick)
                self.assertFalse(self.controller.moving)
        self.assertFalse([c for c in self.brick.sent if "run-" in c])   # never a run-timed / run-direct

    def test_stick_wins_over_held_keys(self):
        left, right = self.drive(["up"], stick=[0, -1])
        self.assertLess(left, 0)   # the stick says backward, the keys forward
        self.assertLess(right, 0)

    def test_stop_clears_the_stick(self):
        self.drive(stick=[0, 1], ticks=3)
        self.assertTrue(self.controller.held or self.controller.stick)   # what /mode checks before re-sending
        with self.controller.lock:
            self.controller.stop()
        self.assertIn(f"echo {rc.HARD_STOP} > L/stop_action", self.brick.sent[-1])
        self.assertIsNone(self.controller.stick)
        self.assertEqual(self.controller.held, set())
        self.assertFalse(self.controller.moving)
        sent = len(self.brick.sent)
        with self.controller.lock:   # /mode after a stop: a gear change must not set the robot off again
            self.controller.set_mode("Fast")
            if self.controller.held or self.controller.stick:
                self.controller._send()
        self.assertEqual(len(self.brick.sent), sent)


class MotorsMovedTest(ControllerTest):
    """The motor cables moved to other sockets: the phone must pick the plugged motors like
    the desktop app does (it used to keep the saved ports and silently drive nothing)."""

    GEAR = rc.wheel_commands({"up"}, "Normal")[0]

    def test_moved_motors_are_picked_saved_and_driven(self):
        self.file.data.update(trim=5, invert_left=True)   # calibration that must survive the save
        want = rc.wheel_commands({"up"}, "Normal", 5)
        target = (-want[0], want[1])                       # Invert on the left motor
        self.assertEqual(self.drive(stick=[0, 1], ticks=self.full_ramp()), target)   # under way on Motor A / D
        self.brick.paths = {"outB": "L2", "outC": "R2"}   # what the reconnect after MotorsChanged found
        last_sent = []

        def save(settings):
            last_sent.append(self.brick.sent[-1])   # the brick's latest command as the file is written
            self.file.save(settings)

        with mock.patch.object(rc, "save_settings", save):
            self.controller._check_ports()
        self.assertEqual(len(self.file.saves), 1)
        saved = self.file.saves[0]
        self.assertEqual((saved["left"], saved["right"]), ("outB", "outC"))
        self.assertEqual((saved["trim"], saved["invert_left"]), (5, True))
        self.assertIn(f"echo {rc.HARD_STOP} > L2/stop_action; echo stop > L2/command", last_sent[0])   # stopped first...
        self.assertIn("echo stop > R2/command", last_sent[0])
        self.assertFalse(self.controller.moving)
        self.assertEqual(self.controller.known_ports, ["outB", "outC"])
        left, right = self.drive(stick=[0, 1], paths=("L2", "R2"))   # ...so the next heartbeat ramps up again
        self.assertLess(left, 0)
        self.assertGreater(right, 0)
        self.assertLess(right, want[1])
        self.assertIsNone(speeds(self.brick.sent[-1]))   # nothing for the old sockets
        self.assertEqual(self.drive(stick=[0, 1], ticks=self.full_ramp(), paths=("L2", "R2")), target)

    def test_unchanged_motors_are_left_alone(self):
        self.drive(stick=[0, 1], ticks=self.full_ramp())
        sent = len(self.brick.sent)
        for _ in range(3):   # the first look at this brick, then nothing new
            self.controller._check_ports()
        self.assertEqual(self.file.saves, [])
        self.assertEqual(len(self.brick.sent), sent)   # no stop
        self.assertTrue(self.controller.moving)
        self.assertEqual(self.controller.known_ports, ["outA", "outD"])
        self.assertEqual(self.drive(stick=[0, 1]), (self.GEAR, self.GEAR))   # still under way, no ramp restart

    def test_saved_pair_still_plugged_in_is_kept(self):
        self.file.data.update(left="outD", right="outA")   # swapped round in Setup: not the brick's order
        self.drive(stick=[0, 1], ticks=self.full_ramp())
        self.controller._check_ports()
        self.brick.paths = {"outA": "L", "outB": "M", "outD": "R"}   # a third motor plugged in
        sent = len(self.brick.sent)
        self.controller._check_ports()
        self.assertEqual(self.file.saves, [])
        self.assertEqual(len(self.brick.sent), sent)   # no stop
        self.assertEqual(self.controller.known_ports, ["outA", "outB", "outD"])
        self.assertEqual(self.controller._ports(), ("outD", "outA"))
        self.assertEqual(self.drive(stick=[0, 1]), (self.GEAR, self.GEAR))

    def test_one_motor_keeps_the_saved_picks_until_a_second_appears(self):
        self.brick.paths = {"outB": "M"}
        self.controller._check_ports()
        self.assertEqual(self.file.saves, [])   # nothing can drive yet: no pair to pick
        self.assertEqual(self.controller._ports(), ("outA", "outD"))
        self.brick.paths = {"outB": "M", "outC": "N"}
        self.controller._check_ports()
        self.assertEqual((self.file.saves[-1]["left"], self.file.saves[-1]["right"]), ("outB", "outC"))

    def test_monitor_reconnects_at_once_when_the_motors_move(self):
        log = []   # (event, the controller's status at it)
        note = lambda event: log.append((event, self.controller.status))
        self.controller.brick = self.brick = MovedBrick(note)
        seen = []   # every telemetry the page could have polled
        update = self.controller._update_telemetry

        def update_and_note():
            update()
            seen.append(dict(self.controller.telemetry))

        self.controller._update_telemetry = update_and_note

        class Enough(Exception):
            """Out of _monitor's forever loop after three rounds of readings."""

        sleeps = []

        def sleep(seconds):
            note(f"sleep {seconds}")
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise Enough

        with mock.patch.object(phone.time, "sleep", sleep), self.assertRaises(Enough):
            self.controller._monitor()
        pause = f"sleep {rc.MONITOR_SECONDS}"
        self.assertEqual([event for event, _ in log],
                         ["read", pause, "motors changed", "drop", "connect", "read", pause, "read", pause])
        self.assertEqual(sleeps, [rc.MONITOR_SECONDS] * 3)   # never the 2 s "Brick disconnected" wait
        for event, status in log:
            self.assertNotIn("disconnected", status.lower(), event)
        for telemetry in seen:
            self.assertTrue(telemetry["connected"], telemetry["status"])
            self.assertNotIn("disconnected", telemetry["status"].lower())
        self.assertEqual(seen[-1]["motors"], "L = Motor B · R = Motor C")
        self.assertEqual(seen[-1]["warning"], "")
        self.assertEqual((self.file.saves[-1]["left"], self.file.saves[-1]["right"]), ("outB", "outC"))


if __name__ == "__main__":
    unittest.main()
