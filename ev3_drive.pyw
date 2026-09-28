"""EV3 RC — drive a two-motor EV3 robot from the keyboard, like an RC car.

Drives the robot over SSH, the same connection the VS Code EV3 extension uses, and
shows a live dashboard: speedometer, per-wheel meters, trip distance, top speed,
link latency and the brick's battery.

Run:   pythonw ev3_drive.pyw               (or double-click the file)
       pythonw ev3_drive.pyw 192.168.0.1   (connect to a specific address)
Needs: pip install paramiko

Calibrate in the SETUP card: pick which motor is each wheel, use Test to check it
rolls forward (Invert it if not), and move Drift fix if the robot curves when it
should go straight. Settings are saved in ev3_drive_settings.json next to this file.

Keys (the window must be focused):
  Up / W      forward            Left / A    turn left
  Down / S    backward           Right / D   turn right
  Up+Left etc. curve             Space       stop
  1 2 3 4     Slow / Normal / Fast / Turbo gear
  + / -       next / previous gear

Turbo drives the motors at raw power (run-direct duty cycle) instead of a regulated
speed: a little faster, but the wheels are no longer speed-matched, so it may drift.

To spare the gears, every gear speeds up gradually (RAMP_SECONDS from stopped to full
power). Letting go of the keys stops with RELEASE_STOP; Space stops with HARD_STOP,
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

import paramiko

HOST = sys.argv[1] if len(sys.argv) > 1 else "ev3dev.local"
USER = "robot"
PASSWORD = "maker"
DEFAULT_LEFT = "outA"     # used until you pick motors in the window
DEFAULT_RIGHT = "outD"
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ev3_drive_settings.json")
MAX_SPEED = 1000         # deg/s at 100 % (EV3 large motor max is ~1050)
MODES = [("Slow", 25), ("Normal", 50), ("Fast", 100), ("Turbo", 100)]   # name, % of top speed
TURBO = "Turbo"          # raw full power instead of speed-regulated
WATCHDOG_CS = 50         # Turbo: stop if no heartbeat for this many 1/100 s
RAMP_SECONDS = 2.5       # time to go from stopped to full power, in every gear (raise for gentler gears)
STEER_SECONDS = 0.5      # time for steering to swing fully (short, so turns respond at once)
# How motors stop: "coast" (roll freely, gentlest), "brake" (short the motor, stops
# quickly) or "hold" (actively drives the wheel back to where it stopped, strongest).
RELEASE_STOP = "brake"   # letting go of the keys
HARD_STOP = "hold"       # Space, the STOP button, window/phone losing focus
TURN_INNER = 0.35        # inner wheel speed while curving, as a fraction of the outer
PULSE_MS = 400           # each drive command runs this long...
RENEW_MS = 120           # ...and is renewed this often while a key is held
MONITOR_SECONDS = 0.2    # how often motor speed/position is read back
WHEEL_MM = 56            # wheel diameter for speed/trip (standard EV3 tyre is 56 mm)
MOTOR_LIMIT = 1050       # ev3dev rejects speed_sp above the motor's max_speed
TRIM_RANGE = 20          # drift fix slider goes from -20 % to +20 %
TEST_SPEED, TEST_MS = 200, 700   # Test button: slow, short nudge of one wheel
BATTERY_RANGE = {"Li-ion": (7.0, 8.3)}   # empty/full volts; AA packs use AA_RANGE
AA_RANGE = (6.0, 9.0)

KEYS = {
    "Up": "up", "w": "up", "W": "up",
    "Down": "down", "s": "down", "S": "down",
    "Left": "left", "a": "left", "A": "left",
    "Right": "right", "d": "right", "D": "right",
}

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
        self.lock = threading.Lock()

    @property
    def connected(self):
        return self.ctl is not None and not self.ctl.closed

    def connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(HOST, username=USER, password=PASSWORD, timeout=8,
                       look_for_keys=False, allow_agent=False)
        transport = client.get_transport()
        transport.set_keepalive(5)
        _, out, _ = client.exec_command(
            'for m in /sys/class/tacho-motor/motor*; do [ -d "$m" ] && echo "$(cat $m/address) $m"; done')
        paths = {}
        for line in out.read().decode().split("\n"):
            if line.strip():
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
            self.client, self.ctl, self.paths = client, ctl, paths
            self.mon, self.mon_out = mon, mon.makefile("r")
        # Brake gives a crisp stop on key release; restored to ev3dev's default on close.
        self.send(" ".join(f"echo brake > {p}/stop_action;" for p in self.paths.values()))

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

    def ramp(self, left_path, right_path, left, right, signs):
        """Move the wheels from their last levels toward `left`/`right` and return the new levels.

        Levels are fractions of full power per wheel, + = forward (before any Invert;
        `signs` is ±1 per wheel for Invert, only needed to read measured speeds). Overall
        speed changes over RAMP_SECONDS but steering (the difference between the wheels)
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
        speed = toward((was_l + was_r) / 2, target_speed, RAMP_SECONDS)
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
            raise ConnectionError("motors changed, reconnecting")
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


def wheel_commands(held, mode, trim=0):
    """Wheel speeds (deg/s, + = forward) for the held directions ("up", "down", "left", "right").

    Shared with ev3_phone.py so the phone drives exactly like this app.
    """
    forward = ("up" in held) - ("down" in held)
    turn = ("right" in held) - ("left" in held)
    top = MOTOR_LIMIT if mode == TURBO else MAX_SPEED
    speed = top * dict(MODES)[mode] // 100
    if forward == 0:                          # spin in place
        left, right = turn * speed, -turn * speed
    else:                                     # drive, slowing the inner wheel to curve
        left = right = forward * speed
        if turn > 0:
            right = int(right * TURN_INNER)
        elif turn < 0:
            left = int(left * TURN_INNER)
    # Drift fix: speed one wheel up and the other down by the same share.
    t = trim / 100
    left, right = left * (1 + t), right * (1 - t)
    clamp = lambda v: int(max(-MOTOR_LIMIT, min(MOTOR_LIMIT, v)))
    return clamp(left), clamp(right)


def send_drive(brick, left_path, right_path, left, right, mode, invert_left=False, invert_right=False):
    """One renewable drive pulse toward wheel speeds from wheel_commands, ramped for the
    gears: speed-regulated, or raw power (with watchdog) in Turbo."""
    # Invert: a motor mounted the other way round needs the opposite command.
    signs = (-1 if invert_left else 1, -1 if invert_right else 1)
    left, right = brick.ramp(left_path, right_path, left / MOTOR_LIMIT, right / MOTOR_LIMIT, signs)
    left, right = left * signs[0], right * signs[1]
    if mode == TURBO:
        return brick.drive_direct(left_path, right_path, round(left * 100), round(right * 100))
    return brick.drive(left_path, right_path, round(left * MOTOR_LIMIT), round(right * MOTOR_LIMIT))


def port_name(port):
    return f"Motor {port[-1]}" if port.startswith("out") else port


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
    """Arrow pad with a round STOP button in the middle; lights up for held directions."""

    def __init__(self, parent, on_press, on_release, on_stop, size=196):
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
        self.moving = False
        self.tick_id = None
        settings = load_settings()
        mode = settings.get("mode", "Normal")
        self.mode = tk.StringVar(value=mode if mode in dict(MODES) else "Normal")
        self.left_port = tk.StringVar(value=settings.get("left", DEFAULT_LEFT))
        self.right_port = tk.StringVar(value=settings.get("right", DEFAULT_RIGHT))
        was_reversed = settings.get("reverse", False)   # older single "Reverse" setting
        self.invert_l = tk.BooleanVar(value=settings.get("invert_left", was_reversed))
        self.invert_r = tk.BooleanVar(value=settings.get("invert_right", was_reversed))
        self.trim = tk.IntVar(value=max(-TRIM_RANGE, min(TRIM_RANGE, settings.get("trim", 0))))
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

        self._build()
        self.bind("<KeyPress>", self._key_down)
        self.bind("<KeyRelease>", self._key_up)
        self.bind("<FocusOut>", lambda e: self._release_all())
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._mode_changed()
        self._dark_title_bar()
        threading.Thread(target=self._monitor, daemon=True).start()
        self.after(40, self._refresh_ui)

    def _dark_title_bar(self):
        """Dark Windows title bar matching the app (Windows 10 20H1+/11; ignored elsewhere)."""
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
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
        self.dpad = DPad(controls, self._press, self._release, self._release_all)
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

        # setup: which motor is which, which way it turns, and drift correction
        setup = self._card()
        head = tk.Frame(setup, bg=CARD)
        head.pack(fill="x", pady=(0, px(6)))
        tk.Label(head, text="SETUP · CALIBRATE", bg=CARD, fg=MUTED, font=FONT_CAPS).pack(side="left")
        Pill(head, "⇄ Swap sides", self._swap_sides, font=FONT_SMALL, padx=px(8), pady=px(2)).pack(
            side="right")
        grid = tk.Frame(setup, bg=CARD)
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
        self.cal_hint = tk.Label(setup, text="Test: the wheel should roll FORWARD.  Wrong wheel → ⇄ Swap.  "
                                             "Rolls backward → Invert.  Curves when going straight → "
                                             "slide Drift fix the other way (double-click it to reset).",
                                 bg=CARD, fg=DIM, font=FONT_SMALL, justify="left", wraplength=px(380))
        self.cal_hint.pack(anchor="w", pady=(px(6), 0))
        self._trim_changed(save=False)
        self.mode.trace_add("write", lambda *a: self._mode_changed())

        tk.Label(self, text="Arrows / WASD to drive  ·  1–4 = gear  ·  Space = stop",
                 bg=BG, fg=MUTED, font=FONT_SMALL).pack(pady=(0, px(10)))

    # ---------- input ----------
    def _key_down(self, event):
        if event.keysym == "space":
            self._release_all()
        elif event.keysym in KEYS:
            self._press(KEYS[event.keysym])
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
        if event.keysym in KEYS:
            self._release(KEYS[event.keysym])

    def _press(self, direction):
        if direction not in self.held:   # ignore Windows key auto-repeat
            self.held.add(direction)
            self._tick()

    def _release(self, direction):
        if direction in self.held:
            self.held.discard(direction)
            self._tick()

    def _release_all(self):
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
        left, right = self._wheel_speeds()
        paths = self.brick.paths
        lp, rp = paths.get(self.left_port.get()), paths.get(self.right_port.get())
        if (left or right) and self.brick.connected and lp and rp and lp != rp:
            send_drive(self.brick, lp, rp, left, right, self.mode.get(),
                       self.invert_l.get(), self.invert_r.get())
            self.moving = True
            self.tick_id = self.after(RENEW_MS, self._tick)
        elif self.moving:
            self.brick.stop(released=True)
            self.moving = False
        self.dpad.show(self.held)

    # ---------- connection & readback ----------
    def _monitor(self):
        while not self.stop_event.is_set():
            try:
                if not self.brick.connected:
                    self.status = ("Connecting…", WARN)
                    self.brick.connect()
                    self.status = ("Connected", GOOD)
                self.readings = self.brick.read_motors()
                self.reading_seq += 1
            except Exception as e:
                self.brick.ctl = None
                self.brick.rtt = None
                self.status = (f"Disconnected: {e}", BAD)
                self.stop_event.wait(2)
                continue
            self.stop_event.wait(MONITOR_SECONDS)

    def _refresh_ui(self):
        text, color = self.status
        self.dot.configure(fg=color)
        self.status_label.configure(text=HOST if color == GOOD else text[:34])
        ports = sorted(self.brick.paths)
        if ports != self.known_ports:
            self._fill_port_menus(ports)
        if self.reading_seq != self.seen_seq:
            self.seen_seq = self.reading_seq
            self._new_reading()
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
            left, right = self.left_port.get(), self.right_port.get()
            if left not in ports:
                left = next(p for p in ports if p != right)
            if right not in ports or right == left:
                right = next(p for p in ports if p != left)
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
        self.port_warning.pack(anchor="w") if warning else self.port_warning.pack_forget()
        self._settings_changed()

    def _settings_changed(self):
        self.focus_set()   # keep arrow keys going to the app after clicking a control
        save_settings({"left": self.left_port.get(), "right": self.right_port.get(),
                       "invert_left": self.invert_l.get(), "invert_right": self.invert_r.get(),
                       "trim": self.trim.get(), "mode": self.mode.get()})

    # ---------- calibration ----------
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
