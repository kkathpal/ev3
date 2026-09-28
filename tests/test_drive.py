"""Offline tests for the drive logic in ev3_drive.pyw: no robot, no SSH, no window.

Run from the project folder:   python -m unittest discover -s tests -v
"""
import os
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

sys.modules.setdefault("paramiko", types.ModuleType("paramiko"))   # not needed offline
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)   # for ev3_config
rc = SourceFileLoader("ev3_drive", os.path.join(ROOT, "ev3_drive.pyw")).load_module()

TICK = rc.RENEW_MS / 1000


class FakeBrick(rc.Brick):
    """A Brick that records the shell commands it would send instead of sending them."""

    def __init__(self):
        super().__init__()
        self.paths = {"outA": "L", "outD": "R"}
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)
        return True


class DriveTest(unittest.TestCase):
    def setUp(self):
        self.brick = FakeBrick()
        self.now = 1000.0
        patcher = mock.patch.object(rc.time, "monotonic", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def drive(self, held, mode, ticks=1, invert=(False, False), tick=TICK):
        """Hold `held` for `ticks` renewals; return the last (left, right) motor command."""
        for _ in range(ticks):
            left, right = rc.wheel_commands(held, mode)
            rc.send_drive(self.brick, "L", "R", left, right, mode, *invert)
            self.now += tick
        attr = "duty_cycle_sp" if mode == rc.TURBO else "speed_sp"
        cmd = self.brick.sent[-1]
        return tuple(int(cmd.split(f" > {p}/{attr}")[0].rsplit("echo ", 1)[1]) for p in ("L", "R"))

    def full_ramp(self):
        return int(rc.RAMP_SECONDS / TICK) + 2

    def test_speeds_up_gradually(self):
        left, right = self.drive({"up"}, "Fast")
        self.assertGreater(left, 0)
        self.assertLess(left, rc.MAX_SPEED * 0.1)
        self.assertEqual(self.drive({"up"}, "Fast", self.full_ramp()), (rc.MAX_SPEED, rc.MAX_SPEED))

    def test_acceleration_does_not_depend_on_command_rate(self):
        # The phone renews faster than the desktop app; it must not accelerate harder.
        slow = self.drive({"up"}, "Fast", 10)
        self.brick.stop(released=True)
        fast = self.drive({"up"}, "Fast", 20, tick=TICK / 2)
        self.assertAlmostEqual(slow[0], fast[0], delta=rc.MOTOR_LIMIT * 0.05)

    def test_curve_from_rest_turns_at_once(self):
        left, right = self.drive({"up", "left"}, "Fast")
        self.assertGreater(left, 0)   # inner wheel never runs backwards
        self.assertAlmostEqual(left / right, rc.TURN_INNER, delta=0.05)

    def test_steering_responds_while_driving(self):
        self.drive({"up"}, "Fast", self.full_ramp())
        left, right = self.drive({"up", "left"}, "Fast", int(rc.STEER_SECONDS / TICK) + 1)
        self.assertAlmostEqual(left / right, rc.TURN_INNER, delta=0.05)
        left, right = self.drive({"up"}, "Fast", int(rc.STEER_SECONDS / TICK) + 1)
        self.assertAlmostEqual(left, right, delta=5)

    def test_spin_in_place(self):
        left, right = self.drive({"left"}, "Normal", int(rc.STEER_SECONDS / TICK) + 1)
        self.assertEqual((left, right), (-rc.MAX_SPEED // 2, rc.MAX_SPEED // 2))

    def test_invert_flips_only_that_motor(self):
        left, right = self.drive({"up"}, "Normal", self.full_ramp(), invert=(True, False))
        self.assertEqual((left, right), (-rc.MAX_SPEED // 2, rc.MAX_SPEED // 2))

    def test_turbo_is_raw_duty_cycle(self):
        self.assertEqual(self.drive({"up"}, rc.TURBO, self.full_ramp()), (100, 100))
        self.assertIn("run-direct", self.brick.sent[-1])
        self.assertIn("$HB", self.brick.sent[-1])   # heartbeat for the brick-side watchdog

    def test_stop_actions_and_ramp_restart(self):
        self.drive({"up"}, "Fast", self.full_ramp())
        self.brick.stop(released=True)
        self.assertIn(f"echo {rc.RELEASE_STOP} > L/stop_action", self.brick.sent[-1])
        self.brick.stop()
        self.assertIn(f"echo {rc.HARD_STOP} > L/stop_action", self.brick.sent[-1])
        left, _ = self.drive({"up"}, "Fast")
        self.assertLess(left, rc.MAX_SPEED * 0.1)   # starts gently again after a stop


if __name__ == "__main__":
    unittest.main()
