"""Offline tests for the drive logic in ev3_drive.pyw: no robot, no SSH, no window.

Run from the project folder:   python -m unittest discover -s tests -v
"""
import io
import itertools
import math
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

    def drive(self, held, mode, ticks=1, invert=(False, False), tick=TICK, ramp=rc.RAMP_SECONDS):
        """Hold `held` for `ticks` renewals; return the last (left, right) motor command."""
        for _ in range(ticks):
            left, right = rc.wheel_commands(held, mode)
            rc.send_drive(self.brick, "L", "R", left, right, mode, *invert, ramp=ramp)
            self.now += tick
        attr = "duty_cycle_sp" if mode == rc.TURBO else "speed_sp"
        cmd = self.brick.sent[-1]
        return tuple(int(cmd.split(f" > {p}/{attr}")[0].rsplit("echo ", 1)[1]) for p in ("L", "R"))

    def full_ramp(self, ramp=rc.RAMP_SECONDS):
        return int(ramp / TICK) + 2

    def first_step(self, ramp=rc.RAMP_SECONDS):
        """The most the first command may reach: one renewal's share of the ramp."""
        return rc.MOTOR_LIMIT * TICK / ramp + 1

    def test_speeds_up_gradually(self):
        left, right = self.drive({"up"}, "Fast")
        self.assertGreater(left, 0)
        self.assertLess(left, self.first_step())
        self.assertLess(left, rc.MAX_SPEED)   # not straight to full speed
        self.assertEqual(self.drive({"up"}, "Fast", self.full_ramp()), (rc.MAX_SPEED, rc.MAX_SPEED))

    def test_acceleration_setting(self):
        gentle = dict(rc.ACCELERATIONS)["Gentle"]
        quick_first = self.drive({"up"}, "Fast")[0]
        self.brick.stop(released=True)
        gentle_first = self.drive({"up"}, "Fast", ramp=gentle)[0]
        self.assertLess(gentle_first, quick_first)   # Gentle speeds up more slowly
        self.assertLess(gentle_first, self.first_step(gentle))
        self.assertEqual(self.drive({"up"}, "Fast", self.full_ramp(gentle), ramp=gentle), (rc.MAX_SPEED,) * 2)
        self.assertEqual(rc.ramp_seconds({"accel": "Gentle"}), gentle)
        self.assertEqual(rc.ramp_seconds({}), rc.RAMP_SECONDS)             # unset: the default
        self.assertEqual(rc.ramp_seconds({"accel": "Warp"}), rc.RAMP_SECONDS)   # unknown: the default

    def test_acceleration_does_not_depend_on_command_rate(self):
        # The phone renews faster than the desktop app; it must not accelerate harder.
        gentle = dict(rc.ACCELERATIONS)["Gentle"]   # long enough that neither reaches full speed
        slow = self.drive({"up"}, "Fast", 10, ramp=gentle)
        self.brick.stop(released=True)
        fast = self.drive({"up"}, "Fast", 20, tick=TICK / 2, ramp=gentle)
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

    def test_horn_plays_in_background_once(self):
        self.assertTrue(self.brick.horn())
        cmd = self.brick.sent[-1]
        self.assertIn(rc.ev3_sound.HORN, cmd)
        self.assertTrue(cmd.rstrip().endswith("&"))   # never blocks the drive command shell
        self.assertIn("timeout", cmd)                 # a stuck player can't pile up
        self.assertNotIn("pgrep", cmd)                # ...or block the next horn
        self.now += 0.1
        self.assertFalse(self.brick.horn())           # a held key doesn't stack horns
        self.assertEqual(len(self.brick.sent), 1)

    def test_horn_works_again_and_again(self):
        for _ in range(5):
            self.assertTrue(self.brick.horn())
            self.now += rc.HORN_GAP + 0.01
        self.assertEqual(len(self.brick.sent), 5)

    def test_stop_actions_and_ramp_restart(self):
        self.drive({"up"}, "Fast", self.full_ramp())
        self.brick.stop(released=True)
        self.assertIn(f"echo {rc.RELEASE_STOP} > L/stop_action", self.brick.sent[-1])
        self.brick.stop()
        self.assertIn(f"echo {rc.HARD_STOP} > L/stop_action", self.brick.sent[-1])
        left, _ = self.drive({"up"}, "Fast")
        self.assertLess(left, self.first_step())   # starts gently again after a stop

    def read(self, lines):
        """read_motors on a fake monitor shell that answers with `lines`."""
        self.brick.mon = types.SimpleNamespace(sendall=lambda data: None)
        self.brick.mon_out = io.StringIO("\n".join(lines) + "\n__END__\n")
        return self.brick.read_motors()

    def test_motor_change_is_noticed_and_drop_leaves_the_motors_alone(self):
        self.assertEqual(self.read(["motors L", "motors R", "bat 7500000 Li-ion", "10 200", "-10 400"]),
                         {"outA": (10, 200), "outD": (-10, 400)})
        self.assertEqual(self.brick.battery, (7.5, "Li-ion"))
        for lines in (["motors L", "motors X", "10 200", "-10 400"],             # a cable moved
                      ["motors L", "motors R", "motors M", "10 200", "-10 400"],   # a third plugged in
                      ["motors L", "10 200"]):                                     # one unplugged
            with self.subTest(lines=lines):
                with self.assertRaises(rc.MotorsChanged) as raised:
                    self.read(lines)
                self.assertIsInstance(raised.exception, ConnectionError)   # other handlers still reconnect
        # drop(): the pulses expire (the watchdog stops Turbo), so no stop command, and connect()
        # gets a fresh watchdog; the old paths stay until it finds the new ones.
        self.brick.ctl = types.SimpleNamespace(closed=False)
        self.brick.drive("L", "R", 100, 100)
        sent = len(self.brick.sent)
        self.brick.drop()
        self.assertEqual(len(self.brick.sent), sent)
        self.assertFalse(self.brick.connected)
        self.assertIsNone(self.brick.mon)
        self.assertEqual(self.brick.paths, {"outA": "L", "outD": "R"})
        with self.assertRaises(ConnectionError):
            self.brick.read_motors()


class CalibrateTest(unittest.TestCase):
    """Calibrate by driving, against every way two motors can be fitted."""

    WIRINGS = [(left_pick_is_left, s0, s1) for left_pick_is_left in (True, False)
               for s0 in (1, -1) for s1 in (1, -1)]

    @staticmethod
    def robot(wiring, left_pick, right_pick):
        """Motor commands for the (left pick, right pick) motors → what the robot's actual
        (left wheel, right wheel) do. s0/s1: which way each motor is mounted."""
        left_pick_is_left, s0, s1 = wiring
        m0, m1 = left_pick * s0, right_pick * s1
        return (m0, m1) if left_pick_is_left else (m1, m0)

    def watch(self, wiring):
        """What a person clicks for each arrow after watching the robot."""
        motion = {pattern: arrow for arrow, pattern in rc.CAL_PATTERNS.items()}
        return {arrow: motion[self.robot(wiring, *pattern)] for arrow, pattern in rc.CAL_PATTERNS.items()}

    def test_every_wiring_drives_right_after_calibrating(self):
        sign = lambda v: (v > 0) - (v < 0)
        for wiring in self.WIRINGS:
            with self.subTest(wiring=wiring):
                (swap, inv_l, inv_r), reason = rc.cal_result(self.watch(wiring))
                self.assertIsNone(reason)
                if swap:   # the left pick is now the motor that was the right pick
                    wiring = (not wiring[0], wiring[2], wiring[1])
                for held in ({"up"}, {"down"}, {"left"}, {"right"}, {"up", "left"}, {"down", "right"}):
                    left, right = rc.wheel_commands(held, "Normal")
                    commands = (-left if inv_l else left, -right if inv_r else right)
                    actual = self.robot(wiring, *commands)
                    self.assertEqual((sign(actual[0]), sign(actual[1])), (sign(left), sign(right)), held)

    def test_prefilled_answers_match_the_current_settings(self):
        for inv_l in (False, True):
            for inv_r in (False, True):
                (swap, got_l, got_r), _ = rc.cal_result(rc.cal_predict(inv_l, inv_r))
                self.assertEqual((swap, got_l, got_r), (False, inv_l, inv_r))

    def test_answers_that_cannot_happen_are_refused(self):
        result, reason = rc.cal_result({"up": "up", "down": "left", "left": "down", "right": "right"})
        self.assertIsNone(result)
        self.assertIn("opposite", reason)
        result, reason = rc.cal_result({"up": "up", "down": None, "left": "left", "right": "right"})
        self.assertIsNone(result)


class StickTest(unittest.TestCase):
    """The phone joystick: exactly the arrows' moves where it points like an arrow, and no
    jump anywhere in between, so the robot never lurches as a thumb slides round."""

    STEP = 0.25   # degrees between sweep samples
    R = math.sqrt(0.5)
    RIM = [(0, 1), (R, R), (1, 0), (R, -R), (0, -1), (-R, -R), (-1, 0), (-R, R)]   # rc.STICK_MOVES' order
    P = float(f"{R:.3f}")   # 0.707: ev3_phone.html sends the stick with toFixed(3), a hair short of the rim
    PHONE = [(0, 1), (P, P), (1, 0), (P, -P), (0, -1), (-P, -P), (-1, 0), (-P, P)]
    TRIMS = (-rc.TRIM_RANGE, -12, -7, 0, 3, 8, rc.TRIM_RANGE)

    @staticmethod
    def at(angle, mode, trim=0):
        """stick_commands for a stick at the rim, `angle` degrees clockwise from straight ahead."""
        a = math.radians(angle)
        return rc.stick_commands(math.sin(a), math.cos(a), mode, trim)

    def test_arrow_directions_drive_like_the_keys(self):
        # Exactly, not within 1: stick_commands round()s before the Drift fix so that the
        # diagonals' sqrt(0.5) noise, and the page's three-decimal stick, still give the keys' speeds.
        for mode in dict(rc.MODES):
            for trim in self.TRIMS:
                for exact, phone, held in zip(self.RIM, self.PHONE, rc.STICK_MOVES):
                    want = rc.wheel_commands(held, mode, trim)
                    for point in (exact, phone):
                        with self.subTest(mode=mode, trim=trim, held=sorted(held), point=point):
                            self.assertEqual(rc.stick_commands(*point, mode, trim), want)

    def test_beyond_the_rim_is_the_rim(self):
        for mode in dict(rc.MODES):
            with self.subTest(mode=mode):
                self.assertEqual(rc.stick_commands(1, 1, mode), rc.stick_commands(self.R, self.R, mode))
                self.assertEqual(rc.stick_commands(0, 3, mode), rc.stick_commands(0, 1, mode))
                self.assertEqual(rc.stick_commands(-2, 0, mode), rc.stick_commands(-1, 0, mode))

    def test_dead_zone(self):
        dz = rc.STICK_DEADZONE
        for mode in dict(rc.MODES):
            with self.subTest(mode=mode):
                self.assertEqual(rc.stick_commands(0, 0, mode), (0, 0))
                self.assertEqual(rc.stick_commands(0, dz, mode), (0, 0))                   # the edge itself
                self.assertEqual(rc.stick_commands(dz * 0.7, -dz * 0.7, mode), (0, 0))   # inside, diagonally
                left, right = rc.stick_commands(0, dz + 0.02, mode)
                self.assertEqual(left, right)
                self.assertGreater(left, 0)
                self.assertLess(left, rc.arrow_speeds({"up"}, mode)[0] * 0.05)   # creeps, no lurch

    def test_reach_sets_the_speed(self):
        halfway = (rc.STICK_DEADZONE + 1) / 2
        for mode in dict(rc.MODES):
            with self.subTest(mode=mode):
                left, right = rc.stick_commands(0, halfway, mode)
                self.assertEqual(left, right)
                self.assertAlmostEqual(left, rc.arrow_speeds({"up"}, mode)[0] / 2, delta=1)

    def test_no_jumps_around_the_circle(self):
        for mode in dict(rc.MODES):
            for trim in (0, 10):
                with self.subTest(mode=mode, trim=trim):
                    # The steepest sector swings a wheel from +top to -top over 45° (spinning right
                    # into curving back-right), so one STEP may move it 2 * top * STEP / 45, with
                    # the Drift fix share on top of that. Rounding adds up to about 2, not 1:
                    # stick_commands round()s each sample (half either way, so up to 1 between
                    # two, and drift_fix scales that by the trim share), then drift_fix int()-
                    # truncates the trimmed value (up to 1 more between two samples).
                    share = 1 + trim / 100
                    top = rc.arrow_speeds({"up"}, mode)[0] * share
                    bound = 2 * top * self.STEP / 45 + share + 1
                    prev = self.at(0, mode, trim)
                    for k in range(1, int(360 / self.STEP) + 1):   # up to and including 360°
                        angle = k * self.STEP
                        cur = self.at(angle, mode, trim)
                        self.assertLessEqual(max(abs(c - p) for c, p in zip(cur, prev)), bound, f"at {angle}°")
                        prev = cur
                    self.assertEqual(prev, self.at(0, mode, trim))   # 360° meets 0°
                    # ...and a hair either side of the axes, closer than the sweep steps
                    for above, below in (((1e-6, 1), (-1e-6, 1)), ((1, 1e-6), (1, -1e-6)), ((-1, 1e-6), (-1, -1e-6))):
                        a, b = rc.stick_commands(*above, mode, trim), rc.stick_commands(*below, mode, trim)
                        self.assertLessEqual(max(abs(p - q) for p, q in zip(a, b)), 1, (above, below))

    def test_spin_direction_holds_across_the_sideways_axis(self):
        # A thumb wobbling around y = 0 while pushing sideways must keep the robot spinning the
        # same way (a design that snapped to the nearest arrow flipped between "right" and
        # "down-right" here).
        for x in (1, -1):
            spin = rc.stick_commands(x, 0, "Normal")
            for y in (0.02, -0.02):
                with self.subTest(x=x, y=y):
                    left, right = rc.stick_commands(x, y, "Normal")
                    self.assertEqual((left > 0, right > 0), (spin[0] > 0, spin[1] > 0))
                    self.assertGreater(min(abs(left), abs(right)), abs(spin[0]) * 0.9)   # still nearly the spin


class PickMotorsTest(unittest.TestCase):
    """Which motors to drive after the cables moved: what DriveApp's Setup card always did,
    now shared with the phone (which used to keep the saved ports and silently send nothing)."""

    NAMES = ("outA", "outB", "outC", "outD")

    @staticmethod
    def desktop_pick(left, right, ports):
        """DriveApp._fill_port_menus' own picking before pick_motors was split out of it
        (git show HEAD:ev3_drive.pyw), so both apps keep choosing exactly as the desktop did."""
        if len(ports) >= 2:
            if left not in ports:
                left = next(p for p in ports if p != right)
            if right not in ports or right == left:
                right = next(p for p in ports if p != left)
        return left, right

    def test_saved_pair_present_is_kept(self):
        self.assertEqual(rc.pick_motors("outA", "outD", ["outA", "outD"]), ("outA", "outD"))
        self.assertEqual(rc.pick_motors("outD", "outA", ["outA", "outD"]), ("outD", "outA"))   # order too
        self.assertEqual(rc.pick_motors("outA", "outD", ["outA", "outB", "outD"]), ("outA", "outD"))

    def test_one_saved_motor_missing_takes_the_other_plugged_one(self):
        self.assertEqual(rc.pick_motors("outA", "outD", ["outB", "outD"]), ("outB", "outD"))
        self.assertEqual(rc.pick_motors("outA", "outD", ["outA", "outC"]), ("outA", "outC"))
        self.assertEqual(rc.pick_motors("outA", "outD", ["outA", "outB", "outC"]), ("outA", "outB"))

    def test_both_saved_motors_missing_takes_the_two_plugged(self):
        self.assertEqual(rc.pick_motors("outA", "outD", ["outB", "outC"]), ("outB", "outC"))   # the user's case
        self.assertEqual(rc.pick_motors("outA", "outD", ["outC", "outB"]), ("outC", "outB"))   # in the brick's order

    def test_same_motor_on_both_sides_is_fixed(self):
        self.assertEqual(rc.pick_motors("outA", "outA", ["outA", "outD"]), ("outA", "outD"))
        self.assertEqual(rc.pick_motors("outD", "outD", ["outA", "outD"]), ("outD", "outA"))
        self.assertEqual(rc.pick_motors("outB", "outB", ["outA", "outD"]), ("outA", "outD"))

    def test_fewer_than_two_motors_keeps_the_saved_picks(self):
        # Nothing can drive yet, and the saved pair must survive a motor being plugged in later.
        for ports in ([], ["outA"], ["outB"]):
            with self.subTest(ports=ports):
                self.assertEqual(rc.pick_motors("outA", "outD", ports), ("outA", "outD"))
                self.assertEqual(rc.pick_motors("outA", "outA", ports), ("outA", "outA"))

    def test_three_motors_plugged(self):
        self.assertEqual(rc.pick_motors("outA", "outD", ["outA", "outC", "outD"]), ("outA", "outD"))
        self.assertEqual(rc.pick_motors("outB", "outC", ["outA", "outC", "outD"]), ("outA", "outC"))
        self.assertEqual(rc.pick_motors("outC", "outB", ["outA", "outC", "outD"]), ("outC", "outA"))
        self.assertEqual(rc.pick_motors("outB", "outB", ["outA", "outC", "outD"]), ("outA", "outC"))

    def test_matches_the_desktop_app_for_every_case(self):
        # Every saved pair (including stale and doubled ones) against every set of plugged motors.
        for left in self.NAMES + ("outX",):
            for right in self.NAMES + ("outX",):
                for n in range(len(self.NAMES) + 1):
                    for ports in itertools.combinations(self.NAMES, n):
                        with self.subTest(saved=(left, right), ports=ports):
                            got = rc.pick_motors(left, right, list(ports))
                            self.assertEqual(got, self.desktop_pick(left, right, list(ports)))
                            if n >= 2:   # ...and always something that can drive
                                self.assertNotEqual(got[0], got[1])
                                self.assertTrue(set(got) <= set(ports), got)


if __name__ == "__main__":
    unittest.main()
