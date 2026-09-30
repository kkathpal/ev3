"""EV3 RC — drive a two-motor EV3 robot from the keyboard, like an RC car.

Drives the robot over SSH, the same connection the VS Code EV3 extension uses, and
shows a live dashboard: speedometer, per-wheel meters, trip distance, top speed,
link latency and the brick's battery.

Run:   pythonw ev3_drive.pyw               (or double-click the file)
       pythonw ev3_drive.pyw 192.168.0.1   (connect to a specific address)
Needs: pip install paramiko
The brick's address and login are in ev3_config.json (see ev3_config.py).

Calibrate in the SETUP card: press Calibrate…, hold each arrow and click what the
robot actually did (it works out Swap and Invert for you). Or by hand: pick which
motor is each wheel, use Test to check it rolls forward (Invert it if not). Move
Drift fix if the robot curves when it should go straight. Settings are saved in
ev3_drive_settings.json next to this file.

Keys (the window must be focused):
  Up / W      forward            Left / A    turn left
  Down / S    backward           Right / D   turn right
  Up+Left etc. curve             Space       stop
  1 2 3 4     Slow / Normal / Fast / Turbo gear
  + / -       next / previous gear
  H           horn
  P           play the sound picked in the SOUND card

Turbo drives the motors at raw power (run-direct duty cycle) instead of a regulated
speed: a little faster, but the wheels are no longer speed-matched, so it may drift.

To spare the gears, every gear speeds up gradually: the Acceleration setting in SETUP
(Quick 0.6 s, Normal 1.2 s or Gentle 2.5 s from stopped to full power). Letting go of the keys stops with RELEASE_STOP; Space stops with HARD_STOP,
which actively holds the wheels still.

Safety: every command runs the motors for only PULSE_MS and is renewed while a key
is held, so the robot stops by itself if keys are released, the window loses focus,
or the connection drops. Turbo has no built-in timeout, so a watchdog on the brick
stops the motors if the app's heartbeat goes quiet for WATCHDOG_CS, or its
connection closes.
"""
import ctypes
import json
import math
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import paramiko
except ImportError:   # a double-clicked .pyw has no console, so say it in a window
    tk.Tk().withdraw()
    messagebox.showerror("EV3", "The 'paramiko' package is missing.\n\nInstall it once with:\n"
                                "    python -m pip install -r requirements.txt")
    sys.exit(1)

import ev3_config
import ev3_find
import ev3_sound

CONFIG = ev3_config.load()
HOST, USER, PASSWORD = CONFIG["host"], CONFIG["user"], CONFIG["password"]
DEFAULT_LEFT = "outA"     # used until you pick motors in the window
DEFAULT_RIGHT = "outD"
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ev3_drive_settings.json")
MAX_SPEED = 1000         # deg/s at 100 % (EV3 large motor max is ~1050)
MODES = [("Slow", 25), ("Normal", 50), ("Fast", 100), ("Turbo", 100)]   # name, % of top speed
TURBO = "Turbo"          # raw full power instead of speed-regulated
WATCHDOG_CS = 50         # Turbo: stop if no heartbeat for this many 1/100 s
# Acceleration (Setup card): time to go from stopped to full power, in every gear. Some
# ramp spares the gears; pick Gentle for a heavy robot or to go easier on them.
ACCELERATIONS = [("Quick", 0.6), ("Normal", 1.2), ("Gentle", 2.5)]
DEFAULT_ACCEL = "Quick"
RAMP_SECONDS = dict(ACCELERATIONS)[DEFAULT_ACCEL]
STEER_SECONDS = 0.5      # time for steering to swing fully (short, so turns respond at once)
# How motors stop: "coast" (roll freely, gentlest), "brake" (short the motor, stops
# quickly) or "hold" (actively drives the wheel back to where it stopped, strongest).
RELEASE_STOP = "brake"   # letting go of the keys
HARD_STOP = "hold"       # Space, the STOP button, window/phone losing focus
STICK_DEADZONE = 0.12    # phone joystick: this much of its reach around the centre does nothing
TURN_INNER = 0.35        # inner wheel speed while curving, as a fraction of the outer
PULSE_MS = 400           # each drive command runs this long...
RENEW_MS = 120           # ...and is renewed this often while a key is held
KEY_RELEASE_MS = 40      # a key release counts only if no press follows within this (auto-repeat)
HORN_GAP = 0.8           # at most one horn per this many seconds
MONITOR_SECONDS = 0.2    # how often motor speed/position is read back
WHEEL_MM = 56            # wheel diameter for speed/trip (standard EV3 tyre is 56 mm)
MOTOR_LIMIT = 1050       # ev3dev rejects speed_sp above the motor's max_speed
TRIM_RANGE = 20          # drift fix slider goes from -20 % to +20 %
TEST_SPEED, TEST_MS = 200, 700   # Test button: slow, short nudge of one wheel (also Calibrate's speed)
BATTERY_RANGE = {"Li-ion": (7.0, 8.3)}   # empty/full volts; AA packs use AA_RANGE
AA_RANGE = (6.0, 9.0)

KEYS = {
    "Up": "up", "w": "up", "W": "up",
    "Down": "down", "s": "down", "S": "down",
    "Left": "left", "a": "left", "A": "left",
    "Right": "right", "d": "right", "D": "right",
}

# Calibrate: each arrow runs the (left pick, right pick) motors this way, with no Invert.
# In wheel terms these are also the motions: "up" = forward, "left" = spin left, ...
CAL_PATTERNS = {"up": (1, 1), "down": (-1, -1), "left": (-1, 1), "right": (1, -1)}
CAL_ARROWS = {"up": "↑", "down": "↓", "left": "←", "right": "→"}
CAL_MOTIONS = {"up": "Forward", "down": "Backward", "left": "Left", "right": "Right"}

BG = "#0f1115"
CARD = "#181b22"
TILE = "#1f232c"
LINE = "#2a2f3a"
FG = "#eef0f4"
MUTED = "#7d8595"
DIM = "#4a5060"
ACCENT = "#5aa9ff"
VIOLET = "#a07bff"
GOOD = "#3ddc97"
WARN = "#ffb547"
BAD = "#ff6363"
TRACK = "#272b35"
HOVER = "#343a47"
GEAR_COLORS = {"Slow": GOOD, "Normal": ACCENT, "Fast": WARN, "Turbo": "#ff4fd8"}

FONT = ("Segoe UI", 9)
FONT_SMALL = ("Segoe UI", 8)
FONT_BOLD = ("Segoe UI Semibold", 9)
FONT_CAPS = ("Segoe UI Semibold", 7)
FONT_TITLE = ("Segoe UI Semibold", 11)
FONT_NUM = ("Bahnschrift", 10)
FONT_NUM_BIG = ("Bahnschrift SemiBold", 13)
FONT_SPEED = ("Bahnschrift SemiBold", 34)
FONT_GEAR = ("Bahnschrift SemiBold", 15)

SCALE = 1.0   # set from the screen DPI when the window is created


def px(v):
    return int(round(v * SCALE))


def blend(c1, c2, t):
    """Mix two #rrggbb colours: t=0 gives c1, t=1 gives c2 (Tk has no alpha)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def deg_to_cm(deg):
    return deg * math.pi * WHEEL_MM / 360 / 10


# Long-lived monitor shell on the brick: only shell builtins per read, lowest CPU
# priority so it never competes with the drive commands or a robot program.
MONITOR_SETUP = r"""
exec 2>/dev/null
renice -n 19 -p $$ >/dev/null
B=/sys/class/power_supply/lego-ev3-battery
read btype < $B/technology
mon() {
  for m in "$@"; do
    read sp < $m/speed; read pos < $m/position; echo "$sp $pos"
  done
  for m in /sys/class/tacho-motor/motor*; do [ -d "$m" ] && echo "motors $m"; done
  read bv < $B/voltage_now; echo "bat $bv $btype"
  echo __END__
}
"""


class MotorsChanged(ConnectionError):
    """A motor was plugged in or out: reconnect at once (drop() first), so the brick-side
    watchdog covers the new motors and the apps can pick the pair again (pick_motors)."""


class Brick:
    """SSH connection with two long-lived shells: `ctl` for drive commands, `mon` for readback."""

    def __init__(self):
        self.client = None
        self.ctl = None
        self.mon = self.mon_out = None
        self.paths = {}          # port -> sysfs motor dir
        self.battery = None      # (volts, technology)
        self.rtt = None          # seconds for the last monitor round trip
        self.levels = {}         # last command per motor path, as a fraction of full power (-1..1)
        self.levels_time = 0.0   # when those were sent (time.monotonic)
        self.coasting = False    # last stop let the motors roll instead of braking
        self.measured = {}       # last read-back speed per motor path, as a fraction of full
        self.last_horn = -HORN_GAP
        self.name = None         # the brick's hostname, read when connecting
        self.address = None      # the IP the connection was made to
        self.lock = threading.Lock()

    @property
    def connected(self):
        return self.ctl is not None and not self.ctl.closed

    def connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        address = ev3_config.ssh_address(HOST)
        client.connect(address, username=USER, password=PASSWORD, timeout=8,
                       look_for_keys=False, allow_agent=False)
        transport = client.get_transport()
        transport.set_keepalive(5)
        _, out, _ = client.exec_command(
            'echo "name $(hostname)"; '
            'for m in /sys/class/tacho-motor/motor*; do [ -d "$m" ] && echo "$(cat $m/address) $m"; done')
        paths, name = {}, None
        for line in out.read().decode().split("\n"):
            if line.startswith("name "):
                name = line[5:].strip() or None   # the brick's own name, e.g. ev3kishan
            elif line.strip():
                addr, path = line.split()
                paths[addr.split(":")[-1]] = path
        if not paths:
            client.close()
            raise RuntimeError("no motors plugged in")

        ctl = transport.open_session()
        ctl.exec_command("exec sh >/dev/null 2>&1")
        ctl.sendall(watchdog_script(paths.values()))
        mon = transport.open_session()
        mon.settimeout(10)
        mon.exec_command("sh")
        mon.sendall(MONITOR_SETUP)

        with self.lock:
            self.client, self.ctl, self.paths, self.name, self.address = client, ctl, paths, name, address
            self.mon, self.mon_out = mon, mon.makefile("r")
        # Brake gives a crisp stop on key release; restored to ev3dev's default on close.
        self.send(" ".join(f"echo brake > {p}/stop_action;" for p in self.paths.values()))

    def drop(self):
        """End this connection without touching the motors, to connect() again straight away.
        Its shells end with it, and so does its watchdog (stopping the motors if in Turbo)."""
        with self.lock:
            client = self.client
            self.client = self.ctl = self.mon = self.mon_out = None
        if client is not None:
            client.close()

    def close(self):
        if self.connected:
            self.send(" ".join(f"echo stop > {p}/command; echo coast > {p}/stop_action;"
                               for p in self.paths.values()))
            time.sleep(0.2)
        with self.lock:
            if self.client is not None:
                self.client.close()
            self.client = self.ctl = self.mon = self.mon_out = None

    def send(self, cmd):
        ctl = self.ctl
        if ctl is None or ctl.closed:
            return False
        try:
            ctl.sendall(cmd + "\n")
            return True
        except Exception:
            self.ctl = None
            return False

    def ramp(self, left_path, right_path, left, right, signs, ramp_seconds=RAMP_SECONDS):
        """Move the wheels from their last levels toward `left`/`right` and return the new levels.

        Levels are fractions of full power per wheel, + = forward (before any Invert;
        `signs` is ±1 per wheel for Invert, only needed to read measured speeds). Overall
        speed changes over `ramp_seconds` (the Acceleration setting) but steering (the difference between the wheels)
        over STEER_SECONDS, so turns respond at once while speeding up stays gentle on the
        gears. Rates are per second, not per command, so the phone's faster heartbeat or
        a burst of key changes can't make it accelerate harder. After a coasting stop the
        wheels may still be rolling, so start from their measured speed.
        """
        now = time.monotonic()
        # Cap the gap so a stall in sending doesn't turn into one big jump.
        elapsed = min(now - self.levels_time, 0.25) if self.levels else RENEW_MS / 1000

        def last(path, sign):
            if path in self.levels:
                return self.levels[path]
            return max(-1.0, min(1.0, self.measured.get(path, 0) * sign)) if self.coasting else 0.0

        def toward(current, target, seconds):
            step = elapsed / seconds
            return current + max(-step, min(step, target - current))

        was_l, was_r = last(left_path, signs[0]), last(right_path, signs[1])
        target_speed, target_steer = (left + right) / 2, (left - right) / 2
        speed = toward((was_l + was_r) / 2, target_speed, ramp_seconds)
        # While speed is still ramping, scale steering with it so a curve keeps its shape
        # (and the inner wheel never runs backwards). Spinning in place has no speed to follow.
        share = 1 if target_speed == 0 else max(0, min(1, speed / target_speed))
        steer = toward((was_l - was_r) / 2, target_steer * share, STEER_SECONDS)
        # Steering wins: if the outer wheel would pass full power, slow the car instead.
        speed = max(abs(steer) - 1, min(1 - abs(steer), speed))
        left, right = speed + steer, speed - steer
        self.levels, self.levels_time, self.coasting = {left_path: left, right_path: right}, now, False
        return left, right

    def drive(self, left_path, right_path, left, right):
        # Set both speeds first, then start both, so the wheels begin together.
        return self.send(
            f"echo {left} > {left_path}/speed_sp; echo {right} > {right_path}/speed_sp; "
            f"echo {PULSE_MS} > {left_path}/time_sp; echo {PULSE_MS} > {right_path}/time_sp; "
            f"echo run-timed > {left_path}/command; echo run-timed > {right_path}/command")

    def drive_direct(self, left_path, right_path, left_duty, right_duty):
        """Turbo: raw power, no timeout, so refresh the watchdog heartbeat with every command."""
        return self.send(
            'read u _ < /proc/uptime; echo "${u%.*}${u#*.}" > $HB; '
            f"echo {left_duty} > {left_path}/duty_cycle_sp; echo {right_duty} > {right_path}/duty_cycle_sp; "
            f"echo run-direct > {left_path}/command; echo run-direct > {right_path}/command")

    def horn(self):
        """Honk, at most once per HORN_GAP seconds (a held key or a repeated tap would stack them)."""
        now = time.monotonic()
        if now - self.last_horn < HORN_GAP:
            return False
        self.last_horn = now
        return self.send(ev3_sound.horn())

    def query_sounds(self):
        """(volume %, [sound paths]) from the brick, on a channel of its own."""
        _, out, _ = self.client.exec_command(ev3_sound.QUERY, timeout=20)
        return ev3_sound.parse(out.read().decode(errors="replace"))

    def stop(self, released=False):
        """Stop every motor, not just the selected pair (the selection may have just changed).

        released=True means the keys were let go (RELEASE_STOP); anything else is an
        explicit stop (HARD_STOP).
        """
        action = RELEASE_STOP if released else HARD_STOP
        sent = self.send("echo 0 > $HB; " + " ".join(
            f"echo {action} > {p}/stop_action; echo stop > {p}/command;" for p in self.paths.values()))
        self.levels.clear()
        self.coasting = action == "coast"
        return sent

    def read_motors(self):
        """Return {port: (speed, position)} for every motor; notices plugged/unplugged motors."""
        with self.lock:
            mon, out = self.mon, self.mon_out
            if mon is None:
                raise ConnectionError("not connected")
            ports = list(self.paths)
            start = time.perf_counter()
            mon.sendall("mon " + " ".join(self.paths[p] for p in ports) + "\n")
            values, present = [], []
            while True:
                line = out.readline()
                if not line:
                    raise ConnectionError("brick closed the connection")
                line = line.strip()
                if line == "__END__":
                    break
                if line.startswith("motors "):
                    present.append(line.split()[1])
                elif line.startswith("bat "):
                    parts = line.split()
                    self.battery = (int(parts[1]) / 1e6, parts[2] if len(parts) > 2 else "")
                elif line:
                    values.append(tuple(int(x) for x in line.split()))
            self.rtt = time.perf_counter() - start
        if sorted(present) != sorted(self.paths.values()):
            raise MotorsChanged("motors changed, reconnecting")
        self.measured = {self.paths[p]: v[0] / MOTOR_LIMIT for p, v in zip(ports, values)}
        return dict(zip(ports, values))


def watchdog_script(paths):
    """Shell run once in the `ctl` shell: sets $HB and starts a background watchdog.

    The heartbeat file holds the brick's uptime in 1/100 s at the last Turbo command
    (0 = not in Turbo). The watchdog stops every motor when that goes stale, and stops
    them and exits when the ctl shell is gone (the SSH connection closed).
    """
    stop = " ".join(f"echo stop > {p}/command;" for p in paths)
    return f"""
for f in /tmp/ev3rc.*.hb; do p=${{f#/tmp/ev3rc.}}; [ -d /proc/${{p%.hb}} ] || rm -f "$f"; done
HB=/tmp/ev3rc.$$.hb; echo 0 > $HB; ctl=$$
(
  trap '' HUP
  while [ -d /proc/$ctl ]; do
    read u _ < /proc/uptime; now=${{u%.*}}${{u#*.}}
    read last < $HB
    # "" = caught mid-rewrite (the app truncates, then writes): skip, don't treat as stale.
    if [ -n "$last" ] && [ "$last" != 0 ] && [ $((now - last)) -gt {WATCHDOG_CS} ]; then
      {stop} echo 0 > $HB
    fi
    sleep 0.2
  done
  read last < $HB; [ "$last" != 0 ] && {{ {stop} }}
  rm -f $HB
) &
"""


def arrow_speeds(held, mode):
    """wheel_commands before Drift fix: the move the held arrows make in this gear."""
    forward = ("up" in held) - ("down" in held)
    turn = ("right" in held) - ("left" in held)
    top = MOTOR_LIMIT if mode == TURBO else MAX_SPEED
    speed = top * dict(MODES)[mode] // 100
    if forward == 0:                          # spin in place
        return turn * speed, -turn * speed
    left = right = forward * speed            # drive, slowing the inner wheel to curve
    if turn > 0:
        right = int(right * TURN_INNER)
    elif turn < 0:
        left = int(left * TURN_INNER)
    return left, right


def drift_fix(left, right, trim):
    """Speed one wheel up and the other down by the same share, within what ev3dev accepts."""
    t = trim / 100
    clamp = lambda v: int(max(-MOTOR_LIMIT, min(MOTOR_LIMIT, v)))
    return clamp(left * (1 + t)), clamp(right * (1 - t))


def wheel_commands(held, mode, trim=0):
    """Wheel speeds (deg/s, + = forward) for the held directions ("up", "down", "left", "right").

    Shared with ev3_phone.py so the phone drives exactly like this app.
    """
    return drift_fix(*arrow_speeds(held, mode), trim)


# The arrows' eight moves by stick direction, clockwise from straight ahead (for stick_commands).
STICK_MOVES = [{"up"}, {"up", "right"}, {"right"}, {"down", "right"},
               {"down"}, {"down", "left"}, {"left"}, {"up", "left"}]


def stick_commands(x, y, mode, trim=0):
    """Wheel speeds (deg/s, + = forward) for a joystick at x (right) and y (forward), each -1..1.

    How far the stick is pushed sets the speed, up to the gear's top at the rim. Its direction
    blends the two nearest arrow moves, so the eight arrow directions drive exactly like the
    keys and the angles between them curve smoothly, with no jump anywhere around the circle.
    Used by ev3_phone.py's joystick.
    """
    reach = min(1.0, math.hypot(x, y))
    if reach <= STICK_DEADZONE:
        return 0, 0
    power = (reach - STICK_DEADZONE) / (1 - STICK_DEADZONE)
    i, share = divmod((math.degrees(math.atan2(x, y)) % 360) / 45, 1)
    a, b = (arrow_speeds(STICK_MOVES[(int(i) + k) % 8], mode) for k in (0, 1))
    # Round, not truncate: at the rim, floating-point noise must still give the keys' exact speeds.
    left, right = (round((p + (q - p) * share) * power) for p, q in zip(a, b))
    return drift_fix(left, right, trim)


def ramp_seconds(settings):
    """The saved Acceleration setting as seconds from stopped to full power."""
    return dict(ACCELERATIONS).get(settings.get("accel"), RAMP_SECONDS)


def send_drive(brick, left_path, right_path, left, right, mode, invert_left=False, invert_right=False,
               ramp=RAMP_SECONDS):
    """One renewable drive pulse toward wheel speeds from wheel_commands, ramped for the
    gears over `ramp` seconds: speed-regulated, or raw power (with watchdog) in Turbo."""
    # Invert: a motor mounted the other way round needs the opposite command.
    signs = (-1 if invert_left else 1, -1 if invert_right else 1)
    left, right = brick.ramp(left_path, right_path, left / MOTOR_LIMIT, right / MOTOR_LIMIT, signs, ramp)
    left, right = left * signs[0], right * signs[1]
    if mode == TURBO:
        return brick.drive_direct(left_path, right_path, round(left * 100), round(right * 100))
    return brick.drive(left_path, right_path, round(left * MOTOR_LIMIT), round(right * MOTOR_LIMIT))


def cal_predict(invert_left, invert_right):
    """What each Calibrate arrow should make the robot do with the current Invert settings,
    as {arrow: motion} (motions use the arrow names: "up" = forward, "left" = spin left)."""
    signs = (-1 if invert_left else 1, -1 if invert_right else 1)
    by_pattern = {pattern: arrow for arrow, pattern in CAL_PATTERNS.items()}
    return {arrow: by_pattern[(a * signs[0], b * signs[1])] for arrow, (a, b) in CAL_PATTERNS.items()}


def cal_result(choice):
    """Turn {arrow: motion the robot did} into ((swap, invert_left, invert_right), None),
    or (None, reason) when the answers don't add up."""
    by_motion = {motion: arrow for arrow, motion in choice.items() if motion}
    if len(by_motion) < len(CAL_PATTERNS):
        return None, "Pick what the robot did for every arrow."
    if {choice["up"], choice["down"]} not in ({"up", "down"}, {"left", "right"}):
        return None, ("↑ and ↓ run the motors exactly opposite, so they must be Forward + Backward\n"
                      "or Left + Right. Test them again, and check both motors are plugged in.")
    forward, spin_right = CAL_PATTERNS[by_motion["up"]], CAL_PATTERNS[by_motion["right"]]
    # Spinning right drives the left wheel forward, so the motor that turns the same way
    # in both is the left wheel. If that's the one picked as right, swap the picks.
    swap = forward[0] != spin_right[0]
    if swap:
        forward = forward[::-1]
    return (swap, forward[0] < 0, forward[1] < 0), None


def port_name(port):
    return f"Motor {port[-1]}" if port.startswith("out") else port


def pick_motors(left, right, ports):
    """The (left, right) motors to drive: the saved picks when the brick has them, otherwise
    others it has, so moving the cables to other sockets still drives. Shared with ev3_phone.py."""
    if len(ports) < 2:
        return left, right   # nothing to drive yet: keep the saved picks
    if left not in ports:
        left = next(p for p in ports if p != right)
    if right not in ports or right == left:
        right = next(p for p in ports if p != left)
    return left, right


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except OSError:
        pass


# ---------------------------------------------------------------- dashboard parts

class Speedometer(tk.Canvas):
    """Segmented 240° speed dial (green → amber → red) with a big readout and gear letter."""

    SEGMENTS = 36
    START, SWEEP = 210, 240

    def __init__(self, parent, max_value, size=210):
        self.size = s = px(size)
        super().__init__(parent, width=s, height=px(size * 0.86), bg=CARD, highlightthickness=0)
        self.max_value = max_value
        self.shown = 0.0
        self.target = 0.0
        cx, cy, r = s / 2, s / 2, s / 2 - px(14)
        w = px(11)
        seg = self.SWEEP / self.SEGMENTS
        self.segs = []
        for i in range(self.SEGMENTS):
            start = self.START - i * seg
            color = blend(GOOD, WARN, i / (self.SEGMENTS * 0.6)) if i < self.SEGMENTS * 0.6 \
                else blend(WARN, BAD, (i - self.SEGMENTS * 0.6) / (self.SEGMENTS * 0.4))
            item = self.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=-(seg - 1.6),
                                   style="arc", outline=TRACK, width=w)
            self.segs.append((item, color))
        # scale labels every 10 cm/s
        for v in range(0, int(max_value) + 1, 10):
            a = math.radians(self.START - self.SWEEP * v / max_value)
            rr = r - px(20)
            self.create_text(cx + rr * math.cos(a), cy - rr * math.sin(a), text=str(v),
                             fill=DIM, font=FONT_SMALL)
        self.value_text = self.create_text(cx, cy - px(4), text="0", fill=FG, font=FONT_SPEED)
        self.create_text(cx, cy + px(26), text="cm/s", fill=MUTED, font=FONT_SMALL)
        self.gear_bg = self.create_oval(cx - px(17), cy + px(42), cx + px(17), cy + px(76),
                                        fill=TILE, outline=LINE, width=px(1))
        self.gear_text = self.create_text(cx, cy + px(59), text="N", fill=MUTED, font=FONT_GEAR)

    def set(self, value, gear, gear_color):
        self.target = max(0.0, value)
        self.itemconfigure(self.gear_text, text=gear, fill=gear_color)
        self.itemconfigure(self.gear_bg, outline=blend(gear_color, CARD, 0.5))

    def animate(self):
        self.shown += (self.target - self.shown) * 0.3
        if abs(self.target - self.shown) < 0.05:
            self.shown = self.target
        lit = round(self.SEGMENTS * min(1.0, self.shown / self.max_value))
        for i, (item, color) in enumerate(self.segs):
            self.itemconfigure(item, outline=color if i < lit else TRACK)
        self.itemconfigure(self.value_text, text=f"{self.shown:.0f}")


class WheelMeter(tk.Canvas):
    """Vertical bar: fills up for forward, down for reverse, from a centre zero line."""

    def __init__(self, parent, label, height=150):
        self.w, self.h = px(26), px(height)
        super().__init__(parent, width=self.w, height=self.h + px(34), bg=CARD, highlightthickness=0)
        self.track = self.create_rectangle(px(7), 0, self.w - px(7), self.h, fill=TILE, outline="")
        self.bar = self.create_rectangle(0, 0, 0, 0, fill=GOOD, outline="")
        self.create_line(px(3), self.h / 2, self.w - px(3), self.h / 2, fill=MUTED, width=px(1))
        self.create_text(self.w / 2, self.h + px(10), text=label, fill=MUTED, font=FONT_CAPS)
        self.value = self.create_text(self.w / 2, self.h + px(25), text="0", fill=FG, font=FONT_SMALL)
        self.shown = 0.0
        self.target = 0.0

    def set(self, deg_per_s):
        self.target = deg_per_s
        self.itemconfigure(self.value, text=f"{deg_per_s:+d}" if deg_per_s else "0")

    def animate(self):
        self.shown += (self.target - self.shown) * 0.3
        frac = max(-1.0, min(1.0, self.shown / MAX_SPEED))
        mid = self.h / 2
        top, bottom = (mid - frac * mid, mid) if frac >= 0 else (mid, mid - frac * mid)
        self.coords(self.bar, px(7), top, self.w - px(7), bottom)
        self.itemconfigure(self.bar, fill=GOOD if frac >= 0 else WARN,
                           state="normal" if abs(frac) > 0.005 else "hidden")


class DPad(tk.Canvas):
    """Arrow pad with a round STOP button in the middle and HORN in the corner (always in
    view, unlike the foldable Sound card); lights up for held directions."""

    def __init__(self, parent, on_press, on_release, on_stop, on_horn, size=196):
        s = px(size)
        super().__init__(parent, width=s, height=s, bg=CARD, highlightthickness=0)
        c, k, gap = s / 2, px(52), px(11)   # centre, key size, gap from the stop button
        stop_r = px(29)
        self.keys = {}
        layout = {"up": (c, c - stop_r - gap - k / 2), "down": (c, c + stop_r + gap + k / 2),
                  "left": (c - stop_r - gap - k / 2, c), "right": (c + stop_r + gap + k / 2, c)}
        arrows = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
        for d, (x, y) in layout.items():
            shape = self._rounded(x - k / 2, y - k / 2, x + k / 2, y + k / 2, px(12),
                                  fill=TILE, outline=LINE, width=px(1), tags=d)
            ax, ay = arrows[d]
            t = px(11)
            pts = [x + ax * t, y + ay * t,
                   x - ax * t * 0.6 + ay * t, y - ay * t * 0.6 + ax * t,
                   x - ax * t * 0.6 - ay * t, y - ay * t * 0.6 - ax * t]
            arrow = self.create_polygon(pts, fill=FG, outline="", tags=d)
            self.keys[d] = (shape, arrow)
            self.tag_bind(d, "<ButtonPress-1>", lambda e, d=d: on_press(d))
            self.tag_bind(d, "<ButtonRelease-1>", lambda e, d=d: on_release(d))
        self.stop_glow = self.create_oval(c - stop_r - px(4), c - stop_r - px(4), c + stop_r + px(4),
                                          c + stop_r + px(4), fill=blend(BAD, CARD, 0.75), outline="",
                                          tags="stop")
        self.create_oval(c - stop_r, c - stop_r, c + stop_r, c + stop_r, fill=BAD, outline="",
                         tags="stop")
        self.create_text(c, c - px(3), text="STOP", fill=BG, font=("Segoe UI Black", 9), tags="stop")
        self.create_text(c, c + px(11), text="space", fill=blend(BG, BAD, 0.4), font=FONT_CAPS,
                         tags="stop")
        self.tag_bind("stop", "<Button-1>", lambda e: on_stop())
        # HORN in the bottom-right corner, between the → and ↓ keys
        hx = hy = c + stop_r + gap + k / 2
        hr = px(23)
        self.horn_button = self.create_oval(hx - hr, hy - hr, hx + hr, hy + hr, fill=blend(WARN, CARD, 0.8),
                                            outline=blend(WARN, CARD, 0.5), width=px(1), tags="horn")
        self.create_text(hx, hy - px(2), text="HORN", fill=WARN, font=FONT_CAPS, tags="horn")
        self.create_text(hx, hy + px(9), text="H", fill=blend(WARN, CARD, 0.4), font=FONT_CAPS, tags="horn")
        self.tag_bind("horn", "<Button-1>", lambda e: on_horn())
        self.configure(cursor="hand2")

    def _rounded(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def show(self, held):
        for d, (shape, arrow) in self.keys.items():
            on = d in held
            self.itemconfigure(shape, fill=ACCENT if on else TILE,
                               outline=blend(ACCENT, "#ffffff", 0.3) if on else LINE)
            self.itemconfigure(arrow, fill=BG if on else FG)


class SignalBars(tk.Canvas):
    """RC-style link quality from the monitor round-trip time."""

    def __init__(self, parent):
        super().__init__(parent, width=px(22), height=px(14), bg=BG, highlightthickness=0)
        self.bars = [self.create_rectangle(px(1 + i * 5), px(14 - 3 - i * 3.5), px(4 + i * 5), px(14),
                                           fill=TRACK, outline="") for i in range(4)]

    def set(self, rtt):
        if rtt is None:
            n, color = 0, TRACK
        else:
            ms = rtt * 1000
            n = 4 if ms < 60 else 3 if ms < 120 else 2 if ms < 250 else 1
            color = GOOD if n >= 3 else WARN if n == 2 else BAD
        for i, b in enumerate(self.bars):
            self.itemconfigure(b, fill=color if i < n else TRACK)


class BatteryIcon(tk.Canvas):
    def __init__(self, parent):
        super().__init__(parent, width=px(26), height=px(13), bg=BG, highlightthickness=0)
        self.create_rectangle(px(1), px(1), px(22), px(12), outline=MUTED, width=px(1))
        self.create_rectangle(px(22), px(4), px(25), px(9), fill=MUTED, outline="")
        self.fill = self.create_rectangle(px(3), px(3), px(3), px(10), fill=GOOD, outline="")

    def set(self, frac):
        color = GOOD if frac > 0.5 else WARN if frac > 0.2 else BAD
        self.coords(self.fill, px(3), px(3), px(3) + (px(20) - px(3)) * frac, px(10))
        self.itemconfigure(self.fill, fill=color)


class TrimSlider(tk.Canvas):
    """Centre-zero slider (tk.Scale draws its knob in the background colour, so it vanishes)."""

    def __init__(self, parent, variable, limit, command, width=160):
        self.w, self.h = px(width), px(20)
        super().__init__(parent, width=self.w, height=self.h, bg=CARD, highlightthickness=0,
                         cursor="hand2")
        self.var, self.limit, self.command = variable, limit, command
        self.pad = px(9)
        mid = self.h / 2
        self.create_line(self.pad, mid, self.w - self.pad, mid, fill=TILE, width=px(4), capstyle="round")
        self.create_line(self.w / 2, mid - px(6), self.w / 2, mid + px(6), fill=MUTED, width=px(1))
        self.fill = self.create_line(self.w / 2, mid, self.w / 2, mid, fill=ACCENT, width=px(4))
        r = px(7)
        self.knob = self.create_oval(0, 0, 2 * r, 2 * r, fill=FG, outline=CARD, width=px(2))
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<Double-Button-1>", lambda e: self._set(0))   # double-click recentres
        self.draw()

    def _x(self, value):
        return self.w / 2 + value / self.limit * (self.w / 2 - self.pad)

    def _drag(self, event):
        span = self.w / 2 - self.pad
        self._set(round((event.x - self.w / 2) / span * self.limit))

    def _set(self, value):
        value = max(-self.limit, min(self.limit, value))
        if value != self.var.get():
            self.var.set(value)
            self.command()
        self.draw()

    def draw(self):
        x, mid, r = self._x(self.var.get()), self.h / 2, px(7)
        self.coords(self.fill, self.w / 2, mid, x, mid)
        self.coords(self.knob, x - r, mid - r, x + r, mid + r)


class Pill(tk.Label):
    """Flat button made from a Label (tk.Button can't be styled this way on Windows)."""

    def __init__(self, parent, text, command, bg=TILE, fg=FG, font=FONT_BOLD, **kw):
        super().__init__(parent, text=text, bg=bg, fg=fg, font=font, cursor="hand2",
                         padx=kw.pop("padx", px(10)), pady=kw.pop("pady", px(5)), **kw)
        self.bind("<Button-1>", lambda e: command())


class CalibrateWindow(tk.Toplevel):
    """Calibrate by driving: hold an arrow, see what the robot does, click it; Save turns
    the answers into Swap/Invert. Keys go through the app's handlers, and while this is
    open the app's _tick sends CAL_PATTERNS (renewed timed pulses) instead of driving."""

    def __init__(self, app):
        super().__init__(app, bg=BG, padx=px(14), pady=px(12))
        self.app = app
        self.title("Calibrate")
        self.resizable(False, False)
        self.transient(app)
        self.attributes("-topmost", True)   # the app is topmost; stay above it
        self.choice = cal_predict(app.invert_l.get(), app.invert_r.get())

        tk.Label(self, text="CALIBRATE BY DRIVING", bg=BG, fg=MUTED, font=FONT_CAPS).pack(anchor="w")
        tk.Label(self, text="Hold an arrow key (or its Test button): the robot moves slowly.\n"
                            "Then click what it actually did.",
                 bg=BG, fg=FG, font=FONT, justify="left").pack(anchor="w", pady=(px(4), px(8)))
        paths = app.brick.paths
        lp, rp = paths.get(app.left_port.get()), paths.get(app.right_port.get())
        if not (app.brick.connected and lp and rp and lp != rp):
            tk.Label(self, text="Connect the brick and pick two different motors first.",
                     bg=BG, fg=WARN, font=FONT_SMALL).pack(anchor="w", pady=(0, px(6)))

        grid = tk.Frame(self, bg=CARD, padx=px(10), pady=px(8))
        grid.pack(fill="x")
        self.arrows, self.chips = {}, {}
        for row, (arrow, symbol) in enumerate(CAL_ARROWS.items()):
            a = tk.Label(grid, text=symbol, bg=TILE, fg=FG, font=FONT_GEAR, width=2)
            a.grid(row=row, column=0, sticky="ns", pady=px(2))
            self.arrows[arrow] = a
            test = Pill(grid, "Test", lambda d=arrow: app._press(d), font=FONT_SMALL, padx=px(8), pady=px(2))
            test.bind("<ButtonRelease-1>", lambda e, d=arrow: app._release(d))
            test.grid(row=row, column=1, sticky="ns", padx=(px(6), px(12)), pady=px(2))
            for col, (motion, name) in enumerate(CAL_MOTIONS.items(), start=2):
                chip = Pill(grid, name, lambda d=arrow, m=motion: self._choose(d, m), font=FONT_SMALL,
                            width=8, pady=px(4))
                chip.grid(row=row, column=col, padx=px(2), pady=px(2))
                self.chips[arrow, motion] = chip

        self.msg = tk.Label(self, text="", bg=BG, fg=MUTED, font=FONT_SMALL, justify="left", anchor="w")
        self.msg.pack(fill="x", pady=(px(8), 0))
        buttons = tk.Frame(self, bg=BG)
        buttons.pack(fill="x", pady=(px(8), 0))
        self.save = Pill(buttons, "Save", self._save)
        self.save.pack(side="right")
        Pill(buttons, "Cancel", self.close).pack(side="right", padx=px(6))

        self.bind("<KeyPress>", app._key_down)
        self.bind("<KeyRelease>", app._key_up)
        self.bind("<Escape>", lambda e: self.close())
        self.bind("<FocusOut>", lambda e: e.widget is self and app._release_all())
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.update_idletasks()   # open over the app's dashboard
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_reqwidth()) // 2
        self.geometry(f"+{max(0, x)}+{app.winfo_rooty() + px(60)}")
        app._dark_title_bar(self)
        self.focus_force()
        self._refresh()

    def test_speeds(self, held):
        """Raw (left pick, right pick) motor speeds for the held arrow. One at a time, so
        what the robot does is clear."""
        if len(held) != 1:
            return 0, 0
        a, b = CAL_PATTERNS[next(iter(held))]
        return a * TEST_SPEED, b * TEST_SPEED

    def show(self, held):
        for arrow, label in self.arrows.items():
            on = arrow in held
            label.configure(bg=ACCENT if on else TILE, fg=BG if on else FG)

    def _choose(self, arrow, motion):
        for other, chosen in self.choice.items():   # each motion belongs to one arrow
            if chosen == motion:
                self.choice[other] = None
        self.choice[arrow] = motion
        self._refresh()

    def _refresh(self):
        for (arrow, motion), chip in self.chips.items():
            on = self.choice[arrow] == motion
            chip.configure(bg=ACCENT if on else TILE, fg=BG if on else FG)
        result, reason = cal_result(self.choice)
        if result:
            self.msg.configure(text="✓ Looks right. Save to use it (Drift fix stays as it is).", fg=GOOD)
        else:
            self.msg.configure(text=reason, fg=WARN if reason.startswith("↑") else MUTED)
        self.save.configure(bg=ACCENT if result else TILE, fg=BG if result else DIM)

    def _save(self):
        result, _ = cal_result(self.choice)
        if result:
            self.close()
            self.app._apply_calibration(*result)

    def close(self):
        self.app.cal_win = None
        self.app._release_all()
        self.destroy()
        self.app.focus_set()


class ConnectWindow(tk.Toplevel):
    """Pick the brick: opens on every start, and from the header's Brick… button. Finds bricks
    over Bluetooth/USB and Wi-Fi (ev3_find: logs in with the brick login to confirm each is an EV3),
    or takes a name or IP address.
    Picking one makes the monitor stop the current brick's motors, let it go and connect."""

    def __init__(self, app):
        super().__init__(app, bg=BG, padx=px(14), pady=px(12))
        self.app = app
        self.title("Connect to the brick")
        self.resizable(False, False)
        self.transient(app)
        self.attributes("-topmost", True)   # the app is topmost; stay above it
        self.results_key = None
        self.refresh_id = None
        wrap = px(390)

        tk.Label(self, text="CONNECT TO THE BRICK", bg=BG, fg=MUTED, font=FONT_CAPS).pack(anchor="w")
        self.status = tk.Label(self, text="", bg=BG, fg=MUTED, font=FONT, justify="left", anchor="w",
                               wraplength=wrap)
        self.status.pack(fill="x", pady=(px(4), px(8)))

        box = tk.Frame(self, bg=CARD, padx=px(10), pady=px(8))
        box.pack(fill="x")
        tk.Label(box, text="BRICK ADDRESS", bg=CARD, fg=MUTED, font=FONT_CAPS).pack(anchor="w")
        tk.Label(box, text="Its name (like ev3kishan) or its IP address: the brick's screen shows its IP at the top.",
                 bg=CARD, fg=DIM, font=FONT_SMALL, justify="left", wraplength=wrap).pack(anchor="w", pady=(px(2), px(6)))
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x")
        self.address = tk.StringVar(value=HOST[:-6] if HOST.endswith(".local") else HOST)
        entry = tk.Entry(row, textvariable=self.address, bg=TILE, fg=FG, insertbackground=FG, relief="flat",
                         highlightthickness=0, font=FONT)
        entry.pack(side="left", fill="x", expand=True, ipady=px(4))
        entry.bind("<Return>", lambda e: self._connect())
        Pill(row, "Connect", self._connect, bg=blend(ACCENT, CARD, 0.6), font=FONT_SMALL, pady=px(4)).pack(
            side="left", padx=(px(6), 0))
        self.remember = tk.BooleanVar(value=True)
        tk.Checkbutton(box, text="Remember on this PC (the phone controller uses it too)", variable=self.remember,
                       bg=CARD, fg=FG, selectcolor=TILE, activebackground=CARD, activeforeground=FG,
                       font=FONT_SMALL, takefocus=False).pack(anchor="w", pady=(px(6), 0))
        self.msg = tk.Label(box, text="", bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left",
                            wraplength=wrap)
        self.msg.pack(fill="x")

        box = tk.Frame(self, bg=CARD, padx=px(10), pady=px(8))
        box.pack(fill="x", pady=(px(8), 0))
        head = tk.Frame(box, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="FIND BRICKS · BLUETOOTH AND WI-FI", bg=CARD, fg=MUTED, font=FONT_CAPS).pack(side="left")
        self.search_button = Pill(head, "Search again", self._search, font=FONT_SMALL, padx=px(8), pady=px(2))
        self.search_button.pack(side="right")
        tk.Label(box, text="Bricks linked to this PC over Bluetooth or USB, then bricks on the same Wi-Fi (the "
                           "brick needs a USB Wi-Fi dongle). Each is checked by logging in with the brick login "
                           f"({USER} / {PASSWORD}). Click one to connect.",
                 bg=CARD, fg=DIM, font=FONT_SMALL, justify="left", wraplength=wrap).pack(anchor="w", pady=(px(4), px(6)))
        self.search_msg = tk.Label(box, text="", bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left",
                                   wraplength=wrap)
        self.search_msg.pack(fill="x")
        self.results = tk.Frame(box, bg=CARD)
        self.results.pack(fill="x", pady=(px(4), 0))

        buttons = tk.Frame(self, bg=BG)
        buttons.pack(fill="x", pady=(px(10), 0))
        Pill(buttons, "Done", self.close, bg=blend(GOOD, CARD, 0.6)).pack(side="right")

        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.update_idletasks()   # open over the app's dashboard
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_reqwidth()) // 2
        self.geometry(f"+{max(0, x)}+{app.winfo_rooty() + px(40)}")
        app._dark_title_bar(self)
        self.focus_force()
        self._search()

    def _search(self):
        self.app.search.start()   # does nothing if one is already running
        self._refresh()

    def _connect(self, address=None):
        if address:
            self.address.set(address)
        error = self.app._connect_to(self.address.get(), self.remember.get())
        self.msg.configure(text=error or f"Connecting to {HOST}… (up to 10 seconds)", fg=BAD if error else MUTED)

    def _refresh(self):
        """Show the connection and the search as they change (every 300 ms while open)."""
        if self.refresh_id is not None:
            self.after_cancel(self.refresh_id)
        text, color = self.app.status
        if color == GOOD:
            name = self.app.brick.name
            text = (f"✓ Connected to {HOST}" + (f" ({name})" if name and f"{name}.local" != HOST else "")
                    + ". Click Done to drive, or pick another brick.")
        self.status.configure(text=text, fg=color)
        state = self.app.search.state
        self.search_button.configure(text="Searching…" if state["running"] else "Search again")
        self.search_msg.configure(text="Searching Bluetooth/USB first, then Wi-Fi, and checking each brick "
                                       "(about 15 seconds)…"
                                  if state["running"] else state.get("error", ""))
        now = self.app.brick.address if color == GOOD else None   # the brick connected right now
        key = (tuple((f["ip"], f["name"], f["via"], f.get("battery"), tuple(f.get("motors", [])))
                     for f in state["found"]), tuple(state.get("paired", [])), now)
        if key != self.results_key:   # rebuild only when the results change
            self.results_key = key
            self._show_results(state["found"], state.get("paired", []), now)
        self.refresh_id = self.after(300, self._refresh)

    def _show_results(self, found, paired, now=None):
        for w in self.results.winfo_children():
            w.destroy()
        for f in found:
            bt = f["via"] != "Wi-Fi"
            row = tk.Frame(self.results, bg=TILE, cursor="hand2")
            row.pack(fill="x", pady=(0, px(4)))
            tag = tk.Label(row, text="BLUETOOTH / USB" if bt else "WI-FI", bg=BG, fg=VIOLET if bt else ACCENT,
                           font=FONT_CAPS, padx=px(6), pady=px(2))
            tag.pack(side="left", padx=px(8), pady=px(6))
            name = tk.Label(row, text=f["name"] or f["ip"], bg=TILE, fg=FG, font=FONT_BOLD)
            name.pack(side="left")
            parts = [w for w in (row, tag, name)]
            if f["ip"] == now:
                mark = tk.Label(row, text="CONNECTED", bg=TILE, fg=GOOD, font=FONT_CAPS)
                mark.pack(side="left", padx=(px(8), 0))
                parts.append(mark)
                row.configure(highlightthickness=px(1), highlightbackground=GOOD)
            motors = " ".join(m[3:] if m.startswith("out") else m for m in f.get("motors", [])) or "none"
            info = tk.Label(row, text=f"{f['ip']}\n{f.get('battery')} V · motors {motors}", bg=TILE, fg=MUTED,
                            font=FONT_SMALL, justify="right")
            info.pack(side="right", padx=px(8))
            parts.append(info)
            for w in parts:
                w.bind("<Button-1>", lambda e, a=f["ip"]: self._connect(a))
        for name in paired:   # paired, but the Bluetooth network to it isn't connected yet
            tk.Label(self.results, text=f"{name} is paired over Bluetooth, but its Bluetooth network isn't "
                                        f"connected yet. On the brick: Wireless and Networks → Tethering → turn "
                                        f"on Bluetooth. On this PC: Devices and Printers → right-click {name} → "
                                        f"Connect using → Access point. Then click Search again.",
                     bg=TILE, fg=MUTED, font=FONT_SMALL, justify="left", anchor="w", wraplength=px(370),
                     padx=px(8), pady=px(6)).pack(fill="x", pady=(0, px(4)))

    def close(self):
        if self.refresh_id is not None:
            self.after_cancel(self.refresh_id)
        self.app.conn_win = None
        self.destroy()
        self.app.focus_set()


# ---------------------------------------------------------------- app

class DriveApp(tk.Tk):
    def __init__(self):
        super().__init__()
        global SCALE
        if sys.platform == "darwin":   # macOS Tk assumes 72 dpi, which draws everything ~25% small
            self.tk.call("tk", "scaling", 96 / 72)
        SCALE = self.winfo_fpixels("1i") / 96
        self.title("EV3 RC")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)

        self.brick = Brick()
        self.held = set()                 # directions currently held
        self.pending_release = {}         # direction (or "play") -> after() id of a delayed key release
        self.play_held = False            # P is down: play once, not on every auto-repeat
        self.moving = False
        self.tick_id = None
        settings = load_settings()
        self.first_run = not settings     # nothing calibrated yet on this computer
        mode = settings.get("mode", "Normal")
        self.mode = tk.StringVar(value=mode if mode in dict(MODES) else "Normal")
        self.left_port = tk.StringVar(value=settings.get("left", DEFAULT_LEFT))
        self.right_port = tk.StringVar(value=settings.get("right", DEFAULT_RIGHT))
        was_reversed = settings.get("reverse", False)   # older single "Reverse" setting
        self.invert_l = tk.BooleanVar(value=settings.get("invert_left", was_reversed))
        self.invert_r = tk.BooleanVar(value=settings.get("invert_right", was_reversed))
        self.trim = tk.IntVar(value=max(-TRIM_RANGE, min(TRIM_RANGE, settings.get("trim", 0))))
        accel = settings.get("accel", DEFAULT_ACCEL)
        self.accel = tk.StringVar(value=accel if accel in dict(ACCELERATIONS) else DEFAULT_ACCEL)
        self.known_ports = []
        self.readings = {}
        self.reading_seq = 0
        self.seen_seq = 0
        self.last_pos = None
        self.trip_cm = 0.0
        self.top_speed = 0.0
        self.drive_seconds = 0.0
        self.last_reading_time = None
        self.status = ("Connecting…", WARN)
        self.stop_event = threading.Event()
        self.sound_info = None     # (volume, [paths]) fetched after connecting, for the UI thread
        self.upload_result = None  # (path, message) from an upload thread, for the UI thread
        self.sounds = []
        self.sound = None
        self.volume = None
        self.home = None           # the robot user's home folder on the brick
        self.sound_note_id = None
        self.cards = {}            # title -> (card, head, title label), for folding
        self.cal_win = None        # CalibrateWindow while it is open
        self.conn_win = None       # ConnectWindow while it is open
        self.brick_host = HOST     # the address the current connection was made to
        self.search = ev3_find.BrickSearch((USER, PASSWORD))   # Find bricks (Bluetooth/USB and Wi-Fi)

        self._build()
        self._fit_screen()
        self.bind("<KeyPress>", self._key_down)
        self.bind("<KeyRelease>", self._key_up)
        self.bind("<FocusOut>", lambda e: self._release_all())
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._mode_changed()
        self._dark_title_bar()
        threading.Thread(target=self._monitor, daemon=True).start()
        self.after(40, self._refresh_ui)
        self.after(300, self._open_connection)   # every start: pick the brick

    def _dark_title_bar(self, window=None):
        """Dark Windows title bar matching the app (Windows 10 20H1+/11; ignored elsewhere)."""
        window = window or self
        try:
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
            r, g, b = (int(BG[i:i + 2], 16) for i in (1, 3, 5))
            color = ctypes.c_int(r | g << 8 | b << 16)   # COLORREF is 0x00bbggrr
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
        except Exception:
            pass

    # ---------- layout ----------
    def _card(self, **pack):
        f = tk.Frame(self, bg=CARD, padx=px(12), pady=px(10))
        f.pack(fill="x", padx=px(10), pady=(0, px(8)), **pack)
        return f

    def _foldable(self, card, head, title):
        """Card title that hides/shows everything under it when clicked (for small screens)."""
        label = tk.Label(head, text="▾  " + title, bg=CARD, fg=MUTED, font=FONT_CAPS, cursor="hand2")
        label.pack(side="left")
        label.bind("<Button-1>", lambda e: self._fold(card, head, label))
        self.cards[title] = (card, head, label)

    def _fold(self, card, head, label):
        folded = getattr(card, "folded", None)
        if folded:
            for w, info in folded:
                w.pack(**info)
            card.folded = None
            head.pack_configure(pady=(0, px(6)))
        else:
            card.folded = [(w, w.pack_info()) for w in card.winfo_children()
                           if w is not head and w.winfo_manager() == "pack"]
            for w, _ in card.folded:
                w.pack_forget()
            head.pack_configure(pady=0)
        label.configure(text=("▾  " if folded else "▸  ") + label.cget("text")[3:])
        self.focus_set()   # keep the keyboard on driving

    def _fit_screen(self):
        """Fold Setup, then Sound, while the window is taller than the screen. On a first
        run Setup is what's needed, so Sound goes first."""
        order = ("SETUP · CALIBRATE", "SOUND")
        for title in reversed(order) if self.first_run else order:
            self.update_idletasks()
            if self.winfo_reqheight() <= self.winfo_screenheight() - px(110):
                return
            self._fold(*self.cards[title])

    def _build(self):
        strip = tk.Canvas(self, height=px(3), bg=BG, highlightthickness=0)
        strip.pack(fill="x")

        def draw_strip(e):
            strip.delete("all")
            for i in range(48):
                strip.create_rectangle(i * e.width / 48, 0, (i + 1) * e.width / 48 + 1, px(3),
                                       outline="", fill=blend(ACCENT, VIOLET, i / 47))
        strip.bind("<Configure>", draw_strip)

        header = tk.Frame(self, bg=BG, padx=px(12), pady=px(10))
        header.pack(fill="x")
        self.dot = tk.Label(header, text="●", bg=BG, fg=WARN, font=("Segoe UI", 10))
        self.dot.pack(side="left")
        tk.Label(header, text="EV3 RC", bg=BG, fg=FG, font=FONT_TITLE).pack(side="left", padx=(px(5), 0))
        self.status_label = tk.Label(header, text="", bg=BG, fg=MUTED, font=FONT)
        self.status_label.pack(side="left", padx=(px(8), 0))
        Pill(header, "📶 Brick…", self._open_connection, font=FONT_SMALL, padx=px(8), pady=px(2)).pack(
            side="left", padx=(px(8), 0))
        self.bat_text = tk.Label(header, text="", bg=BG, fg=MUTED, font=FONT_NUM)
        self.bat_text.pack(side="right")
        self.bat_icon = BatteryIcon(header)
        self.bat_icon.pack(side="right", padx=(px(12), px(4)))
        self.ping_text = tk.Label(header, text="", bg=BG, fg=MUTED, font=FONT_NUM)
        self.ping_text.pack(side="right")
        self.signal = SignalBars(header)
        self.signal.pack(side="right", padx=(0, px(4)))

        # dashboard: wheel meter | speedometer | wheel meter
        dash = self._card()
        self.meter_l = WheelMeter(dash, "LEFT")
        self.meter_l.pack(side="left", anchor="s")
        self.speedo = Speedometer(dash, deg_to_cm(MOTOR_LIMIT * 1.15))   # headroom for Turbo
        self.speedo.pack(side="left", expand=True)
        self.meter_r = WheelMeter(dash, "RIGHT")
        self.meter_r.pack(side="right", anchor="s")

        # trip / top speed
        trip = tk.Frame(self, bg=CARD, padx=px(12), pady=px(8))
        trip.pack(fill="x", padx=px(10), pady=(0, px(8)))
        self.trip_vals = {}
        for name in ("TRIP", "TOP SPEED", "DRIVE TIME"):
            cell = tk.Frame(trip, bg=CARD)
            cell.pack(side="left", expand=True, fill="x")
            tk.Label(cell, text=name, bg=CARD, fg=DIM, font=FONT_CAPS).pack(anchor="w")
            v = tk.Label(cell, text="–", bg=CARD, fg=FG, font=FONT_NUM_BIG)
            v.pack(anchor="w")
            self.trip_vals[name] = v
        Pill(trip, "Reset", self._reset_trip, font=FONT_SMALL, padx=px(8), pady=px(3)).pack(
            side="right", anchor="center")

        # controls: d-pad | gears
        controls = self._card()
        self.dpad = DPad(controls, self._press, self._release, self._release_all, self.brick.horn)
        self.dpad.pack(side="left")
        gears = tk.Frame(controls, bg=CARD)
        gears.pack(side="left", expand=True, fill="both", padx=(px(14), 0))
        tk.Label(gears, text="GEAR", bg=CARD, fg=MUTED, font=FONT_CAPS).pack(anchor="w", pady=(0, px(4)))
        self.mode_buttons = {}
        for i, (name, pct) in enumerate(MODES, start=1):
            b = tk.Frame(gears, bg=TILE, cursor="hand2")
            b.pack(fill="x", pady=(0, px(6)))
            num = tk.Label(b, text=str(i), bg=TILE, fg=MUTED, font=FONT_GEAR, width=2)
            num.pack(side="left", padx=(px(6), 0), pady=px(4))
            txt = tk.Frame(b, bg=TILE)
            txt.pack(side="left", padx=(px(4), px(8)))
            n = tk.Label(txt, text=name.upper(), bg=TILE, fg=FG, font=FONT_BOLD)
            n.pack(anchor="w")
            caption = ("full power · may drift" if name == TURBO else
                       f"{pct}% · {deg_to_cm(MAX_SPEED * pct / 100):.0f} cm/s")
            p = tk.Label(txt, text=caption, bg=TILE, fg=MUTED, font=FONT_SMALL)
            p.pack(anchor="w")
            for w in (b, num, txt, n, p):
                w.bind("<Button-1>", lambda e, m=name: self.mode.set(m))
            self.mode_buttons[name] = (b, num, txt, n, p)

        self._build_sound()

        # setup: which motor is which, which way it turns, and drift correction
        setup = self._card()
        head = tk.Frame(setup, bg=CARD)
        head.pack(fill="x", pady=(0, px(6)))
        self._foldable(setup, head, "SETUP · CALIBRATE")
        Pill(head, "⇄ Swap sides", self._swap_sides, font=FONT_SMALL, padx=px(8), pady=px(2)).pack(
            side="right")
        Pill(head, "Calibrate…", self._open_calibration, bg=blend(ACCENT, CARD, 0.6), font=FONT_SMALL,
             padx=px(8), pady=px(2)).pack(side="right", padx=(0, px(6)))
        grid = self.setup_grid = tk.Frame(setup, bg=CARD)
        grid.pack(fill="x")
        self.port_menus, self.motor_labels = {}, {}
        for i, (name, var, inv, side) in enumerate((("Left wheel", self.left_port, self.invert_l, "left"),
                                                    ("Right wheel", self.right_port, self.invert_r, "right"))):
            tk.Label(grid, text=name, bg=CARD, fg=MUTED, font=FONT).grid(row=i, column=0, sticky="w")
            tk.Checkbutton(grid, text="Invert", variable=inv, bg=CARD, fg=FG, selectcolor=TILE,
                           activebackground=CARD, activeforeground=FG, font=FONT_SMALL, takefocus=False,
                           command=self._settings_changed).grid(row=i, column=2, sticky="w")
            Pill(grid, "Test", lambda s=side: self._test_wheel(s), font=FONT_SMALL, padx=px(8),
                 pady=px(2)).grid(row=i, column=3, padx=(px(6), 0))
            shown = tk.StringVar(value="–")   # "Motor A"; `var` holds the port id ("outA")
            menu = tk.OptionMenu(grid, shown, "")
            menu.configure(bg=TILE, fg=FG, activebackground=ACCENT, activeforeground=BG,
                           highlightthickness=0, bd=0, relief="flat", font=FONT_BOLD, width=8,
                           takefocus=False, indicatoron=False, padx=px(8), pady=px(3))
            menu["menu"].configure(bg=CARD, fg=FG, activebackground=ACCENT, font=FONT)
            menu.grid(row=i, column=1, sticky="w", padx=px(8), pady=px(2))
            val = tk.Label(grid, text="–", bg=CARD, fg=MUTED, font=FONT_NUM, anchor="e", width=8)
            val.grid(row=i, column=4, sticky="e")
            self.port_menus[name] = (menu, var, shown)
            self.motor_labels[name] = (val, var)
        grid.columnconfigure(4, weight=1)
        self.port_warning = tk.Label(setup, text="", bg=CARD, fg=WARN, font=FONT_SMALL)
        self.port_warning.pack(anchor="w")
        for var in (self.left_port, self.right_port):
            var.trace_add("write", lambda *a: self._ports_changed())

        drift = tk.Frame(setup, bg=CARD)
        drift.pack(fill="x", pady=(px(8), 0))
        tk.Label(drift, text="Drift fix", bg=CARD, fg=MUTED, font=FONT).pack(side="left")
        self.trim_label = tk.Label(drift, text="", bg=CARD, fg=FG, font=FONT_NUM, width=15, anchor="e")
        self.trim_label.pack(side="right")
        TrimSlider(drift, self.trim, TRIM_RANGE, self._trim_changed).pack(side="left", padx=px(8))
        accel = tk.Frame(setup, bg=CARD)
        accel.pack(fill="x", pady=(px(8), 0))
        tk.Label(accel, text="Acceleration", bg=CARD, fg=MUTED, font=FONT).pack(side="left")
        self.accel_buttons = {}
        for name, seconds in ACCELERATIONS:
            b = Pill(accel, f"{name} · {seconds:g} s", lambda n=name: self.accel.set(n), font=FONT_SMALL,
                     padx=px(8), pady=px(2))
            b.pack(side="left", padx=(px(6), 0))
            self.accel_buttons[name] = b
        self.accel.trace_add("write", lambda *a: self._accel_changed())
        self._accel_changed(save=False)
        self.cal_hint = tk.Label(setup, text="Calibrate… drives each arrow and asks what the robot did.  "
                                             "Test: wheel should roll FORWARD (wrong wheel → ⇄ Swap, "
                                             "backward → Invert).  Curves going straight → slide Drift fix "
                                             "the other way (double-click resets).",
                                 bg=CARD, fg=DIM, font=FONT_SMALL, justify="left", wraplength=px(380))
        self.cal_hint.pack(anchor="w", pady=(px(6), 0))
        self._trim_changed(save=False)
        self.mode.trace_add("write", lambda *a: self._mode_changed())

        tk.Label(self, text="Arrows / WASD to drive  ·  1–4 = gear  ·  H = horn  ·  P = play sound  ·  Space = stop",
                 bg=BG, fg=MUTED, font=FONT_SMALL).pack(pady=(0, px(10)))

    def _build_sound(self):
        """Sound card: built-in/uploaded sounds, horn, speech and volume (see ev3_sound)."""
        f = self._card()
        head = tk.Frame(f, bg=CARD)
        head.pack(fill="x", pady=(0, px(6)))
        self._foldable(f, head, "SOUND")
        Pill(head, "+", lambda: self._change_volume(ev3_sound.VOLUME_STEP), font=FONT_SMALL,
             padx=px(7), pady=px(1)).pack(side="right")
        self.vol_label = tk.Label(head, text="–", bg=CARD, fg=FG, font=FONT_NUM, width=4)
        self.vol_label.pack(side="right")
        Pill(head, "−", lambda: self._change_volume(-ev3_sound.VOLUME_STEP), font=FONT_SMALL,
             padx=px(7), pady=px(1)).pack(side="right")
        tk.Label(head, text="VOLUME", bg=CARD, fg=DIM, font=FONT_CAPS).pack(side="right", padx=(0, px(6)))

        row = tk.Frame(f, bg=CARD)
        row.pack(fill="x")
        self.sound_pick = Pill(row, "Pick a sound  ▾", self._sound_menu, font=FONT_SMALL, anchor="w",
                               pady=px(4))
        self.sound_pick.pack(side="left", fill="x", expand=True)
        Pill(row, "▶ Play", self._play_sound, bg=blend(GOOD, CARD, 0.6), font=FONT_SMALL,
             pady=px(4)).pack(side="left", padx=(px(6), 0))
        Pill(row, "■", self._stop_sound, font=FONT_SMALL, pady=px(4)).pack(side="left", padx=(px(4), 0))
        Pill(row, "HORN", self.brick.horn, bg=blend(WARN, CARD, 0.72), fg=WARN, font=FONT_SMALL,
             pady=px(4)).pack(side="left", padx=(px(4), 0))

        row = tk.Frame(f, bg=CARD)
        row.pack(fill="x", pady=(px(6), 0))
        self.say_text = tk.StringVar()
        self.say_entry = tk.Entry(row, textvariable=self.say_text, bg=TILE, fg=FG, insertbackground=FG,
                                  relief="flat", highlightthickness=0, font=FONT_SMALL)
        self.say_entry.pack(side="left", fill="x", expand=True, ipady=px(4))
        self.say_entry.bind("<Return>", lambda e: self._say())
        self.say_entry.bind("<Escape>", lambda e: self.focus_set())
        Pill(row, "Say", self._say, font=FONT_SMALL, pady=px(4)).pack(side="left", padx=(px(6), 0))
        Pill(row, "Upload…", self._upload_sound, font=FONT_SMALL, pady=px(4)).pack(
            side="left", padx=(px(4), 0))
        self.sound_note = tk.Label(f, text="Type above and press Enter to make the robot talk (it's kept in my sounds)",
                                   bg=CARD, fg=DIM, font=FONT_SMALL, anchor="w")
        self.sound_note.pack(fill="x", pady=(px(4), 0))

    # ---------- input ----------
    def _key_down(self, event):
        if event.widget is self.say_entry:   # typing for Say, not driving
            return
        if event.keysym == "space":
            self._release_all()
        elif event.keysym in KEYS:
            direction = KEYS[event.keysym]
            if direction in self.pending_release:   # auto-repeat, not a real release
                self.after_cancel(self.pending_release.pop(direction))
            self._press(direction)
        elif event.keysym in ("h", "H"):
            self.brick.horn()
        elif event.keysym in ("p", "P"):
            if "play" in self.pending_release:   # auto-repeat on macOS/Linux
                self.after_cancel(self.pending_release.pop("play"))
            elif not self.play_held:              # auto-repeat on Windows
                self._play_sound()
            self.play_held = True
        elif not event.char:   # Shift, Ctrl, ... ("" is "in" every string below)
            return
        elif event.char in "1234"[:len(MODES)]:
            self.mode.set(MODES[int(event.char) - 1][0])
        elif event.char in "+=-_":
            names = [n for n, _ in MODES]
            step = 1 if event.char in "+=" else -1
            i = max(0, min(len(names) - 1, names.index(self.mode.get()) + step))
            self.mode.set(names[i])

    def _key_up(self, event):
        if event.widget is self.say_entry:
            return
        if event.keysym in ("p", "P"):   # delayed like the drive keys below, so holding P plays once
            self.pending_release["play"] = self.after(
                KEY_RELEASE_MS, lambda: (self.pending_release.pop("play", None),
                                         setattr(self, "play_held", False)))
            return
        # Holding a key on macOS/Linux auto-repeats as release+press pairs, which would
        # brake and restart the ramp many times a second. Release a moment later instead;
        # a press that follows at once cancels it.
        if event.keysym in KEYS:
            direction = KEYS[event.keysym]
            if direction in self.pending_release:
                self.after_cancel(self.pending_release[direction])
            self.pending_release[direction] = self.after(
                KEY_RELEASE_MS, lambda: (self.pending_release.pop(direction, None), self._release(direction)))

    def _press(self, direction):
        if direction not in self.held:   # ignore key auto-repeat
            self.held.add(direction)
            self._tick()

    def _release(self, direction):
        if direction in self.held:
            self.held.discard(direction)
            self._tick()

    def _release_all(self):
        for after_id in self.pending_release.values():
            self.after_cancel(after_id)
        self.pending_release.clear()
        self.play_held = False
        self.held.clear()
        self._tick()
        self.brick.stop()   # always, even if we think we're already stopped

    # ---------- driving ----------
    def _wheel_speeds(self):
        return wheel_commands(self.held, self.mode.get(), self.trim.get())

    def _tick(self):
        """Send the current drive state now, then keep renewing it while keys are held."""
        if self.tick_id is not None:
            self.after_cancel(self.tick_id)
            self.tick_id = None
        calibrating = self.cal_win is not None
        left, right = self.cal_win.test_speeds(self.held) if calibrating else self._wheel_speeds()
        paths = self.brick.paths
        lp, rp = paths.get(self.left_port.get()), paths.get(self.right_port.get())
        if (left or right) and self.brick.connected and lp and rp and lp != rp:
            if calibrating:   # raw pattern, no Invert; slow enough to skip the ramp, like Test
                self.brick.drive(lp, rp, left, right)
            else:
                send_drive(self.brick, lp, rp, left, right, self.mode.get(),
                           self.invert_l.get(), self.invert_r.get(), dict(ACCELERATIONS)[self.accel.get()])
            self.moving = True
            self.tick_id = self.after(RENEW_MS, self._tick)
        elif self.moving:
            self.brick.stop(released=True)
            self.moving = False
        self.dpad.show(self.held)
        if calibrating:
            self.cal_win.show(self.held)

    # ---------- connection & readback ----------
    def _monitor(self):
        while not self.stop_event.is_set():
            try:
                if self.brick.connected and self.brick_host != HOST:
                    self.brick.close()   # another brick was picked: stop this one and let it go
                if not self.brick.connected:
                    self.status = (f"Connecting to {HOST}…", WARN)
                    self.brick_host = HOST
                    self.brick.connect()
                    self.status = ("Connected", GOOD)
                    try:
                        self.sound_info = self.brick.query_sounds()
                    except Exception:
                        pass   # sound list is optional; driving still works
                self.readings = self.brick.read_motors()
                self.reading_seq += 1
            except MotorsChanged:
                self.brick.drop()   # reconnect now; _refresh_ui then picks the pair (pick_motors)
                continue
            except Exception as e:
                self.brick.ctl = None
                self.brick.rtt = None
                self.status = (f"Can't reach {HOST}: {e}", BAD)
                self.stop_event.wait(2)
                continue
            self.stop_event.wait(MONITOR_SECONDS)

    def _refresh_ui(self):
        text, color = self.status
        self.dot.configure(fg=color)
        self.status_label.configure(text=(self.brick.name or HOST) if color == GOOD else text[:34])
        ports = sorted(self.brick.paths)
        if ports != self.known_ports:
            self._fill_port_menus(ports)
        if self.reading_seq != self.seen_seq:
            self.seen_seq = self.reading_seq
            self._new_reading()
        self._sound_updates()
        self.speedo.animate()
        self.meter_l.animate()
        self.meter_r.animate()
        self.after(40, self._refresh_ui)

    def _new_reading(self):
        """Update the dashboard from the latest motor readings (runs ~5x a second)."""
        for val, var in self.motor_labels.values():
            r = self.readings.get(var.get())
            val.configure(text=f"{r[0]:+d}°/s" if r else "–")

        sign_l = -1 if self.invert_l.get() else 1
        sign_r = -1 if self.invert_r.get() else 1
        left = self.readings.get(self.left_port.get())
        right = self.readings.get(self.right_port.get())
        if left and right and self.left_port.get() != self.right_port.get():
            ls, rs = left[0] * sign_l, right[0] * sign_r   # in robot terms: + is forward
            self.meter_l.set(ls)
            self.meter_r.set(rs)
            forward = deg_to_cm((ls + rs) / 2)
            spin = abs(ls - rs) > 40 and abs(ls + rs) < abs(ls - rs) / 2
            if spin:
                gear, gcolor = ("↻" if ls > rs else "↺"), VIOLET
            elif forward > 0.5:
                gear, gcolor = "D", GOOD
            elif forward < -0.5:
                gear, gcolor = "R", WARN
            else:
                gear, gcolor = "N", MUTED
            speed = abs(forward) if not spin else deg_to_cm((abs(ls) + abs(rs)) / 2)
            self.speedo.set(speed, gear, gcolor)
            self.top_speed = max(self.top_speed, speed)
            now = time.monotonic()
            if speed > 0.5 and self.last_reading_time is not None:
                self.drive_seconds += min(1.0, now - self.last_reading_time)
            self.last_reading_time = now

            pos = (left[1] * sign_l, right[1] * sign_r)
            if self.last_pos is not None:
                dl, dr = pos[0] - self.last_pos[0], pos[1] - self.last_pos[1]
                if abs(dl) < 5000 and abs(dr) < 5000:   # ignore jumps from position resets
                    self.trip_cm += abs(deg_to_cm((dl + dr) / 2))
            self.last_pos = pos

        self.trip_vals["TRIP"].configure(
            text=f"{self.trip_cm / 100:.2f} m" if self.trip_cm >= 100 else f"{self.trip_cm:.0f} cm")
        self.trip_vals["TOP SPEED"].configure(text=f"{self.top_speed:.0f} cm/s")
        m, sec = divmod(int(self.drive_seconds), 60)
        self.trip_vals["DRIVE TIME"].configure(text=f"{m}:{sec:02d}")

        self.signal.set(self.brick.rtt)
        self.ping_text.configure(text=f"{self.brick.rtt * 1000:.0f} ms" if self.brick.rtt else "–")
        if self.brick.battery:
            volts, tech = self.brick.battery
            lo, hi = BATTERY_RANGE.get(tech, AA_RANGE)
            frac = max(0.0, min(1.0, (volts - lo) / (hi - lo)))
            self.bat_icon.set(frac)
            self.bat_text.configure(text=f"{frac:.0%}")

    def _reset_trip(self):
        self.trip_cm = 0.0
        self.top_speed = 0.0
        self.drive_seconds = 0.0
        self._new_reading()
        self.focus_set()

    # ---------- motor selection ----------
    def _fill_port_menus(self, ports):
        """Offer the motors the brick actually has; keep saved picks when they're present."""
        self.known_ports = ports
        for menu, var, _ in self.port_menus.values():
            m = menu["menu"]
            m.delete(0, "end")
            for port in ports:
                m.add_command(label=port_name(port), command=lambda p=port, v=var: v.set(p))
        if len(ports) >= 2:
            left, right = pick_motors(self.left_port.get(), self.right_port.get(), ports)
            self.left_port.set(left)
            self.right_port.set(right)
        self._ports_changed()

    def _ports_changed(self):
        self._release_all()   # never keep driving on a pair that just changed
        self.last_pos = None
        for _, var, shown in self.port_menus.values():
            shown.set(port_name(var.get()) + "  ▾" if var.get() in self.known_ports else "–")
        left, right = self.left_port.get(), self.right_port.get()
        if len(self.known_ports) < 2:
            warning = "Plug in two motors to drive" if self.known_ports else ""
        elif left == right:
            warning = "Pick two different motors"
        else:
            warning = ""
        self.port_warning.configure(text=warning)
        if warning:
            card, head, label = self.cards["SETUP · CALIBRATE"]
            if getattr(card, "folded", None):
                self._fold(card, head, label)   # the fix is in there: show it
            self.port_warning.pack(anchor="w", after=self.setup_grid)
        else:
            self.port_warning.pack_forget()
        self._settings_changed()

    def _settings_changed(self):
        self.focus_set()   # keep arrow keys going to the app after clicking a control
        save_settings({"left": self.left_port.get(), "right": self.right_port.get(),
                       "invert_left": self.invert_l.get(), "invert_right": self.invert_r.get(),
                       "trim": self.trim.get(), "mode": self.mode.get(), "accel": self.accel.get()})

    # ---------- calibration ----------
    def _open_connection(self):
        if self.conn_win is not None:
            self.conn_win.lift()
            return
        self._release_all()
        self.conn_win = ConnectWindow(self)

    def _connect_to(self, address, remember=False):
        """Switch to another brick by name or IP address. Returns an error message, or ""."""
        global HOST
        host = ev3_config.parse_address(address)
        if not host:
            return "Type the brick's name (like ev3kishan) or its IP address (like 192.168.1.23)."
        self._release_all()
        HOST = host   # Brick.connect reads it; the monitor closes the old brick and connects this one
        self.status = (f"Connecting to {host}…", WARN)
        self.last_pos = None
        if remember:
            try:
                ev3_config.save_host(host)   # the phone controller and the next start use it too
            except OSError as e:
                return f"Connecting, but couldn't remember it: {e.strerror or e}"
        return ""

    def _open_calibration(self):
        if self.cal_win is not None:
            self.cal_win.lift()
            return
        self._release_all()
        self.cal_win = CalibrateWindow(self)

    def _apply_calibration(self, swap, invert_left, invert_right):
        if swap:
            self._swap_sides()
        self.invert_l.set(invert_left)
        self.invert_r.set(invert_right)
        self._settings_changed()
        inv = lambda on: " (inverted)" if on else ""
        self.cal_hint.configure(
            text=f"Calibrated: left wheel = {port_name(self.left_port.get())}{inv(invert_left)}, right wheel = "
                 f"{port_name(self.right_port.get())}{inv(invert_right)}.  Drive ↑ to check it.", fg=GOOD)

    def _swap_sides(self):
        left, right = self.left_port.get(), self.right_port.get()
        self.left_port.set(right)
        self.right_port.set(left)

    def _test_wheel(self, side):
        """Nudge one wheel slowly 'forward' so you can see which wheel it is and which way it turns."""
        self._release_all()
        port = (self.left_port if side == "left" else self.right_port).get()
        path = self.brick.paths.get(port)
        if not path or not self.brick.connected:
            self.cal_hint.configure(text="Not connected to the brick yet.", fg=WARN)
            return
        invert = (self.invert_l if side == "left" else self.invert_r).get()
        speed = -TEST_SPEED if invert else TEST_SPEED
        self.brick.send(f"echo {speed} > {path}/speed_sp; echo {TEST_MS} > {path}/time_sp; "
                        f"echo run-timed > {path}/command")
        self.cal_hint.configure(
            text=f"Testing {port_name(port)} as the {side.upper()} wheel: it should roll FORWARD.  "
                 f"Other wheel moved → ⇄ Swap.  Rolled backward → tick Invert.", fg=FG)
        self.focus_set()

    def _accel_changed(self, save=True):
        for name, b in self.accel_buttons.items():
            on = name == self.accel.get()
            b.configure(bg=blend(ACCENT, CARD, 0.35) if on else TILE, fg=BG if on else FG)
        if save:
            self._settings_changed()

    def _trim_changed(self, save=True):
        t = self.trim.get()
        self.trim_label.configure(text="0 · straight" if t == 0 else
                                  f"left wheel +{t}%" if t > 0 else f"right wheel +{-t}%")
        if self.held:
            self._tick()   # apply while driving so you can tune it live
        if save:
            self._settings_changed()

    def _mode_changed(self):
        for name, widgets in self.mode_buttons.items():
            active = name == self.mode.get()
            color = GEAR_COLORS[name]
            bg = blend(color, CARD, 0.78) if active else TILE
            frame, num, txt, n, p = widgets
            for w in widgets:
                w.configure(bg=bg)
            num.configure(fg=color if active else MUTED)
            n.configure(fg=FG if active else MUTED)
            frame.configure(highlightthickness=px(1), highlightbackground=color if active else TILE)
        if self.held:
            self._tick()   # apply the new speed immediately while driving
        self._settings_changed()

    # ---------- sound ----------
    def _note(self, text, seconds=4):
        """Show a message under the sound controls for a few seconds."""
        if self.sound_note_id is not None:
            self.after_cancel(self.sound_note_id)
        self.sound_note.configure(text=text, fg=MUTED)
        self.sound_note_id = self.after(int(seconds * 1000),
                                        lambda: self.sound_note.configure(text="", fg=DIM))

    def _sound_menu(self):
        if not self.sounds:
            self._note("No sounds yet: waiting for the brick")
            return
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT, activeforeground=BG)
        for group, paths in ev3_sound.grouped(self.sounds):
            sub = tk.Menu(menu, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT, activeforeground=BG)
            for path in paths:
                sub.add_command(label=ev3_sound.name(path), command=lambda p=path: self._pick_sound(p))
            menu.add_cascade(label=group, menu=sub)
        w = self.sound_pick
        menu.tk_popup(w.winfo_rootx(), w.winfo_rooty() + w.winfo_height())

    def _pick_sound(self, path, play=True):
        self.sound = path
        self.sound_pick.configure(text=f"{ev3_sound.group(path)} · {ev3_sound.name(path)}  ▾")
        if play:
            self._play_sound()

    def _play_sound(self):
        if self.sound is None:
            self._sound_menu()
            return
        self.brick.send(ev3_sound.play(self.sound))
        self._note(f"Playing {ev3_sound.name(self.sound)}")

    def _stop_sound(self):
        self.brick.send(ev3_sound.STOP)

    def _say(self):
        text = self.say_text.get().strip()
        self.focus_set()   # hand the keyboard back to driving
        if not text:
            self._note("Type something for the robot to say first")
            return
        if self.home:   # keep it, so it joins "my sounds" and ▶ Play / P replays it
            cmd, path = ev3_sound.say_and_keep(text, self.home)
            self.brick.send(cmd)
            if path not in self.sounds:
                self.sounds.append(path)
            self._pick_sound(path, play=False)
            self._note(f"Saying “{text}” · saved to my sounds")
        else:
            self.brick.send(ev3_sound.say(text))
            self._note(f"Saying “{text}”")

    def _change_volume(self, delta):
        if self.volume is None:
            return
        self.volume = max(0, min(100, self.volume + delta))
        self.vol_label.configure(text=f"{self.volume}%")
        self.brick.send(ev3_sound.set_volume(self.volume))

    def _upload_sound(self):
        path = filedialog.askopenfilename(parent=self, title="Upload a sound to the EV3",
                                          filetypes=[("WAV sound", "*.wav"), ("All files", "*.*")])
        self.focus_set()
        if not path:
            return
        if not self.brick.connected:
            self._note("Not connected to the brick yet")
            return
        self._note(f"Uploading {os.path.basename(path)}…", 120)
        client = self.brick.client
        threading.Thread(target=lambda: setattr(self, "upload_result", ev3_sound.upload(client, path)),
                         daemon=True).start()

    def _sound_updates(self):
        """Apply what the background threads fetched (runs on the UI thread)."""
        if self.sound_info is not None:
            volume, sounds, self.home = self.sound_info
            self.sound_info = None
            self.sounds = sounds + [s for s in self.sounds if s not in sounds]   # keep fresh uploads
            if volume is not None:
                self.volume = volume
                self.vol_label.configure(text=f"{volume}%")
        if self.upload_result is not None:
            path, message = self.upload_result
            self.upload_result = None
            if path:
                if path not in self.sounds:
                    self.sounds.append(path)
                self._pick_sound(path, play=False)   # ready for ▶ Play
            self._note(message, 6)

    def _close(self):
        self.stop_event.set()
        self.held.clear()
        try:
            self.brick.close()
        finally:
            self.destroy()


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # crisp text on scaled displays
    except Exception:
        pass
    DriveApp().mainloop()
