"""Offline tests: with two phones on the phone page, one drives at a time.

Run from the project folder:   python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_phone import ControllerTest, phone, speeds   # noqa: E402  (same fixtures)

A, B = "phoneA", "phoneB"


class OneDriverTest(ControllerTest):
    def moves(self):
        """How many drive commands (not stops) the robot got so far."""
        return sum(1 for cmd in self.brick.sent if speeds(cmd))

    def stopped(self):
        return bool(self.brick.sent) and speeds(self.brick.sent[-1]) is None

    def test_second_phone_cannot_drive_while_the_first_does(self):
        self.assertTrue(self.controller.drive(["up"], client=A))
        before = self.moves()
        self.now += 0.1
        self.assertFalse(self.controller.drive([], stick=[0.5, 0.5], client=B))   # ignored
        self.assertFalse(self.controller.drive(["left"], client=B))
        self.assertEqual(self.moves(), before)
        self.assertEqual(self.controller.held, {"up"})   # still A's input
        self.assertEqual(self.controller.control(A), "you")
        self.assertEqual(self.controller.control(B), "other")

    def test_the_other_phone_going_away_does_not_stop_the_robot(self):
        self.controller.drive(["up"], client=A)
        sent = len(self.brick.sent)
        self.controller.request_stop(B)                  # B locked its screen / switched app
        self.assertEqual(len(self.brick.sent), sent)     # A keeps driving
        self.controller.request_stop(B, explicit=True)   # B pressed STOP: always stops
        self.assertTrue(self.stopped())

    def test_controls_free_up_after_the_driver_lets_go(self):
        self.controller.drive(["up"], client=A)
        self.now += 0.1
        self.controller.drive([], client=A)              # A lets go
        self.now += phone.DRIVER_IDLE / 2
        self.assertFalse(self.controller.drive(["up"], client=B))
        self.now += phone.DRIVER_IDLE
        self.assertEqual(self.controller.control(B), "free")
        self.assertTrue(self.controller.drive(["up"], client=B))
        self.assertEqual(self.controller.control(B), "you")
        self.assertEqual(self.controller.control(A), "other")

    def test_take_control_stops_then_hands_over(self):
        self.controller.drive(["up"], client=A)
        self.controller.take_over(B)
        self.assertTrue(self.stopped())
        self.assertFalse(self.controller.drive(["up"], client=A))   # A is locked out now
        self.assertTrue(self.controller.drive(["left"], client=B))

    def test_gear_and_calibration_tests_follow_the_driver(self):
        self.controller.drive(["up"], mode="Normal", client=A)
        self.controller.drive(["up"], mode="Fast", client=B)        # ignored, gear included
        self.assertEqual(self.controller.mode, "Normal")
        sent = len(self.brick.sent)
        self.controller.cal_test("up", client=B)                    # a calibration test moves it too
        self.assertEqual(len(self.brick.sent), sent)

    def test_page_ids(self):
        self.assertEqual(phone.client_id("k3j2h1-abc"), "k3j2h1-abc")
        for bad in (None, "", 42, "x" * 65, "a b", "a;b"):
            self.assertIsNone(phone.client_id(bad), bad)


if __name__ == "__main__":
    unittest.main()
