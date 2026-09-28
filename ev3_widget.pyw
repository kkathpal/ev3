"""EV3 status widget for Windows.

Shows live details of an ev3dev brick (battery, system, ports, motors, sensors)
in a small always-on-top window, with buttons to free memory on the brick and
to jog motors / switch sensor modes. Data is read over SSH, the same connection
the VS Code EV3 extension uses.

Run:   pythonw ev3_widget.pyw            (or double-click the file)
       pythonw ev3_widget.pyw 192.168.0.1   (connect to a specific address)
Needs: pip install paramiko
The brick's address and login are in ev3_config.json (see ev3_config.py).

Drag the title bar to move it. Right-click for options.
Hold ◀ / ▶ next to a motor to run it; release to stop. ⟳ next to a sensor switches its mode.
SOUND plays the brick's built-in effects or your own WAV files (Upload… copies them to
~/sounds on the brick), speaks typed text, and sets the volume.
The memory buttons run as root via sudo, using the robot user's password.
"""
import collections
import ctypes
import math
import os
import queue
import shlex
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
import ev3_sound

CONFIG = ev3_config.load()
HOST, USER, PASSWORD = CONFIG["host"], CONFIG["user"], CONFIG["password"]
AUTO_REFRESH_SECONDS = 3.0   # AUTO mode: light on the brick
LIVE_REFRESH_SECONDS = 0.3   # LIVE mode, and while a motor button is held
INFO_EVERY = 30              # refresh slow-changing info (IP, disk) every N polls
MOTOR_SPEED = 400            # deg/s for the jog buttons (large motor max is ~1050)
JOG_PULSE_MS = 800           # each jog command runs this long; renewed while held,
JOG_RENEW_MS = 400           # so the motor stops by itself if the connection drops

# Sent once per connection to a long-lived `sh` on the brick. poll() uses only shell
# builtins (read/echo/case), so each refresh starts no new processes on the brick's
# slow CPU. info() may fork, but runs rarely. Shell variables persist between polls,
# which lets poll() remember the running program's PID instead of rescanning /proc.
# The shell drops itself to the lowest CPU priority (nice 19) so it never slows a robot
# program: measured ~25% faster busy loops while the widget polls. Programs keep
# ev3dev's default nice -10; raising them to -20 starved sensor updates instead.
SHELL_SETUP = r"""
exec 2>/dev/null
renice -n 19 -p $$ >/dev/null
B=/sys/class/power_supply/lego-ev3-battery
pp=""; prog=""; last_scan=-99
info() {
  . /etc/os-release
  echo "host=$(hostname)"
  echo "os=$PRETTY_NAME"
  echo "kernel=$(uname -r)"
  echo "ip=$(hostname -I)"
  echo "disk=$(df -k / | awk 'NR==2{print $2, $3}')"
  read t < $B/technology; echo "bat_type=$t"
""" + ev3_sound.QUERY + r"""
  echo __END__
}
poll() {
  read up _ < /proc/uptime; echo "uptime=$up"
  read l1 l5 l15 _ < /proc/loadavg; echo "load=$l1 $l5 $l15"
  while read k v _; do
    case $k in
      MemTotal:) mt=$v;; MemAvailable:) ma=$v;; Buffers:) bu=$v;; Cached:) ca=$v;;
      SwapTotal:) swt=$v;; SwapFree:) swf=$v; break;;
    esac
  done < /proc/meminfo
  echo "mem=$mt $ma $((bu + ca)) $swt $((swt - swf))"
  read bv < $B/voltage_now; read bi < $B/current_now; echo "bat=$bv $bi"
  # Scanning every process is the costliest part of a poll: re-check the known PID,
  # and only rescan at most every 3 s.
  c=""; [ -n "$pp" ] && read c < /proc/$pp/comm
  if [ "$c" != pybricks-microp ]; then
    pp=""; prog=""
    if [ $((${up%.*} - last_scan)) -ge 3 ]; then
      last_scan=${up%.*}
      for f in /proc/[0-9]*/comm; do
        read c < $f || continue
        if [ "$c" = pybricks-microp ]; then
          f=${f%/comm}; pp=${f#/proc/}; read prog < $f/cmdline; break
        fi
      done
    fi
  fi
  echo "program=${prog#pybricks-micropython}"
  for p in /sys/class/lego-port/port*; do
    read a < $p/address; read s < $p/status; echo "port=$a|$s"
  done
  for m in /sys/class/tacho-motor/motor*; do
    [ -d "$m" ] || continue
    read a < $m/address; read d < $m/driver_name; read pos < $m/position
    read sp < $m/speed; read st < $m/state
    echo "motor=$a|$d|$pos|$sp|$st|$m"
  done
  for s in /sys/class/lego-sensor/sensor*; do
    [ -d "$s" ] || continue
    read a < $s/address; read d < $s/driver_name; read mo < $s/mode; read mos < $s/modes
    read dec < $s/decimals; read u < $s/units; read n < $s/num_values
    v=""; i=0
    while [ $i -lt $n ]; do read x < $s/value$i; v="$v $x"; i=$((i + 1)); done
    echo "sensor=$a|$d|$mo|$dec|$u|$v|$s|$mos"
  done
  echo __END__
}
"""

# Memory buttons: (needs root, shell command). Commands must be plain sh.
DROP_CACHES = "sync; echo 3 > /proc/sys/vm/drop_caches"
ACTIONS = {
    "clear": (True, DROP_CACHES),
    # Swap on ev3dev is zram (compressed RAM), so there is nothing to pull back from it;
    # never swapoff it: `swapon -a` can't restore it, only zram_swap.service can.
    "optimize": (True, DROP_CACHES + "; echo 1 > /proc/sys/vm/compact_memory"),
    "stop": (False, "pkill -f '[p]ybricks-micropython' && echo stopped=1"),
}


def action_script(name):
    """Wrap an action so it reports free RAM before and after."""
    as_root, cmd = ACTIONS[name]
    if as_root:
        cmd = f"echo {shlex.quote(PASSWORD)} | sudo -S -p '' sh -c {shlex.quote(cmd)}"
    return f"""
renice -n 19 -p $$ >/dev/null
before=$(awk '/MemFree/{{print $2}}' /proc/meminfo)
{cmd}
echo "rc=$?"
sleep 1
echo "freed=$(( $(awk '/MemFree/{{print $2}}' /proc/meminfo) - before ))"
"""


# Empty/full voltage per battery type. The brick's own *_design values are off by 10x.
BATTERY_RANGE = {"Li-ion": (7.0, 8.3)}
AA_RANGE = (6.0, 9.0)

PORTS = ["in1", "in2", "in3", "in4", "outA", "outB", "outC", "outD"]

DEVICE_NAMES = {
    "lego-ev3-l-motor": "Large motor",
    "lego-ev3-m-motor": "Medium motor",
    "lego-nxt-motor": "NXT motor",
    "lego-ev3-touch": "Touch",
    "lego-ev3-color": "Color",
    "lego-ev3-us": "Ultrasonic",
    "lego-ev3-gyro": "Gyro",
    "lego-ev3-ir": "Infrared",
    "lego-nxt-touch": "NXT touch",
    "lego-nxt-light": "NXT light",
    "lego-nxt-sound": "NXT sound",
    "lego-nxt-us": "NXT ultrasonic",
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

FONT = ("Segoe UI", 9)
FONT_SMALL = ("Segoe UI", 8)
FONT_BOLD = ("Segoe UI Semibold", 9)
FONT_CAPS = ("Segoe UI Semibold", 7)
FONT_TITLE = ("Segoe UI Semibold", 11)
FONT_NUM = ("Bahnschrift", 10)
FONT_NUM_BIG = ("Bahnschrift SemiBold", 12)
FONT_GAUGE = ("Bahnschrift SemiBold", 17)

# COL-COLOR readings -> name, swatch colour
COLOR_NAMES = {0: ("No color", DIM), 1: ("Black", "#202020"), 2: ("Blue", "#3b7bff"),
               3: ("Green", "#2ecc71"), 4: ("Yellow", "#f7d038"), 5: ("Red", "#ff4d4d"),
               6: ("White", "#f5f5f5"), 7: ("Brown", "#9a6332")}

SCALE = 1.0   # set from the screen DPI when the window is created


def px(v):
    return int(round(v * SCALE))


def blend(c1, c2, t):
    """Mix two #rrggbb colours: t=0 gives c1, t=1 gives c2 (Tk has no alpha)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def parse(text):
    info = {"ports": {}, "motors": {}, "sensors": {}}
    for line in text.splitlines():
        key, _, val = line.partition("=")
        if key == "port":
            addr, status = val.split("|", 1)
            info["ports"][addr.split(":")[-1]] = status
        elif key == "motor":
            addr, driver, pos, speed, state, path = val.split("|")
            info["motors"][addr.split(":")[-1]] = {
                "driver": driver, "position": int(pos), "speed": int(speed),
                "state": state, "path": path,
            }
        elif key == "sensor":
            addr, driver, mode, decimals, units, values, path, modes = val.split("|")
            scale = 10 ** int(decimals or 0)
            info["sensors"][addr.split(":")[-1]] = {
                "driver": driver, "mode": mode, "units": units, "path": path,
                "modes": modes.split(), "values": [int(v) / scale for v in values.split()],
            }
        elif key:
            info[key] = val.strip()
    return info


class Poller(threading.Thread):
    """Background thread: keeps one SSH connection and a long-lived shell on the brick.

    A second shell (`ctl`) takes motor/sensor commands from the UI thread, so they
    never wait behind a poll.
    """

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out
        self.client = None
        self.sh = self.sh_out = self.ctl = None
        self.interval = AUTO_REFRESH_SECONDS
        self.wake = threading.Event()
        self.actions = queue.Queue()

    def request(self, name):
        """Queue a memory action; it runs on this thread before the next poll."""
        self.actions.put(name)
        self.wake.set()

    def upload(self, local_path):
        """Queue copying a WAV file to the brick's sounds folder (runs on this thread)."""
        self.actions.put(("upload", local_path))
        self.wake.set()

    def send(self, cmd):
        """Fire-and-forget command on the brick (called from the UI thread)."""
        ctl = self.ctl
        if ctl is not None and not ctl.closed:
            try:
                ctl.sendall(cmd + "\n")
            except Exception:
                pass

    def _connect(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(HOST, username=USER, password=PASSWORD, timeout=8,
                            look_for_keys=False, allow_agent=False)
        transport = self.client.get_transport()
        transport.set_keepalive(5)
        self.sh = transport.open_session()
        self.sh.settimeout(15)
        self.sh.exec_command("sh")
        self.sh.sendall(SHELL_SETUP)
        self.sh_out = self.sh.makefile("r")
        self.ctl = transport.open_session()
        self.ctl.exec_command("exec sh >/dev/null 2>&1")

    def _disconnect(self):
        self.ctl = None
        if self.client is not None:
            self.client.close()
        self.client = self.sh = self.sh_out = None

    def _query(self, func):
        self.sh.sendall(func + "\n")
        lines = []
        while True:
            line = self.sh_out.readline()
            if not line:
                raise ConnectionError("brick closed the connection")
            line = line.rstrip("\n")
            if line == "__END__":
                return "\n".join(lines)
            lines.append(line)

    def _run(self, script):
        _, stdout, _ = self.client.exec_command(script, timeout=30)
        return stdout.read().decode(errors="replace")

    def run(self):
        info, polls = {}, 0
        while True:
            try:
                if self.client is None:
                    self.out.put(("status", f"Connecting to {HOST}…"))
                    self._connect()
                    polls = 0
                while not self.actions.empty():
                    name = self.actions.get()
                    if isinstance(name, tuple):   # ("upload", local path)
                        self.out.put(("uploaded", ev3_sound.upload(self.client, name[1])))
                        polls = 0   # re-read info so the new sound shows up in the list
                        continue
                    result = dict(line.split("=", 1) for line in self._run(action_script(name)).split()
                                  if "=" in line)
                    self.out.put(("action", (name, result)))
                if polls % INFO_EVERY == 0:
                    info = parse(self._query("info"))
                polls += 1
                data = parse(self._query("poll"))
                self.out.put(("data", {**info, **data}))
            except Exception as e:
                self._disconnect()
                self.out.put(("error", str(e) or type(e).__name__))
                self.wake.wait(3)
                self.wake.clear()
                continue
            self.wake.wait(self.interval)
            self.wake.clear()


class Gauge(tk.Canvas):
    """270° ring gauge with rounded ends; the arc eases toward each new value."""

    def __init__(self, parent, label, size=92):
        self.size = s = px(size)
        super().__init__(parent, width=s, height=s, bg=CARD, highlightthickness=0)
        self.w = px(7)
        pad = self.w / 2 + px(3)
        self.box = (pad, pad, s - pad, s - pad)
        self.r = (s - 2 * pad) / 2
        self.value = self.target = 0.0
        self.color = ACCENT
        self._anim = None
        self.create_arc(*self.box, start=225, extent=-270, style="arc", outline=TRACK, width=self.w)
        for angle in (225, -45):
            self._cap(angle, TRACK)
        self.arc = self.create_arc(*self.box, start=225, extent=-1, style="arc", outline=ACCENT,
                                   width=self.w, state="hidden")
        self.cap_a = self._cap(225, ACCENT, hidden=True)
        self.cap_b = self._cap(225, ACCENT, hidden=True)
        self.text = self.create_text(s / 2, s / 2 - px(3), text="–", fill=FG, font=FONT_GAUGE)
        self.sub = self.create_text(s / 2, s / 2 + px(17), text="", fill=MUTED, font=FONT_SMALL)
        self.create_text(s / 2, s - px(7), text=label.upper(), fill=MUTED, font=FONT_CAPS)

    def _point(self, angle):
        a = math.radians(angle)
        return self.size / 2 + self.r * math.cos(a), self.size / 2 - self.r * math.sin(a)

    def _cap(self, angle, color, hidden=False):
        x, y = self._point(angle)
        h = self.w / 2
        return self.create_oval(x - h, y - h, x + h, y + h, fill=color, outline="",
                                state="hidden" if hidden else "normal")

    def _move_cap(self, item, angle):
        x, y = self._point(angle)
        h = self.w / 2
        self.coords(item, x - h, y - h, x + h, y + h)

    def set(self, fraction, text, sub="", color=ACCENT):
        self.target = max(0.0, min(1.0, fraction))
        self.color = color
        self.itemconfigure(self.text, text=text)
        self.itemconfigure(self.sub, text=sub)
        if self._anim is None:
            self._step()

    def _step(self):
        self.value += (self.target - self.value) * 0.25
        if abs(self.target - self.value) < 0.002:
            self.value = self.target
        extent = -270 * self.value
        state = "normal" if self.value > 0.004 else "hidden"
        self.itemconfigure(self.arc, extent=min(-0.1, extent), outline=self.color, state=state)
        for cap, angle in ((self.cap_a, 225), (self.cap_b, 225 + extent)):
            self._move_cap(cap, angle)
            self.itemconfigure(cap, fill=self.color, state=state)
        self._anim = self.after(16, self._step) if self.value != self.target else None


class Spark(tk.Canvas):
    """Scrolling history graph; the first series gets a filled area underneath."""

    def __init__(self, parent, height=46, samples=120):
        super().__init__(parent, height=px(height), bg=CARD, highlightthickness=0)
        self.samples = samples
        self.series = []   # (deque of 0..1, colour)
        self.bind("<Configure>", lambda e: self.redraw())

    def add(self, color):
        self.series.append((collections.deque(maxlen=self.samples), color))
        return len(self.series) - 1

    def push(self, index, value):
        self.series[index][0].append(max(0.0, min(1.0, value)))

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        for frac in (0.25, 0.5, 0.75):
            y = h - frac * (h - 4) - 2
            self.create_line(0, y, w, y, fill=blend(LINE, CARD, 0.4), dash=(2, 4))
        step = w / (self.samples - 1)
        for i, (values, color) in enumerate(self.series):
            if len(values) < 2:
                continue
            x0 = w - (len(values) - 1) * step
            pts = []
            for j, v in enumerate(values):
                pts += [x0 + j * step, h - v * (h - 4) - 2]
            if i == 0:
                self.create_polygon(x0, h, *pts, w, h, fill=blend(color, CARD, 0.78), outline="")
            self.create_line(*pts, fill=color, width=px(2), smooth=True)
            self.create_oval(pts[-2] - px(3), pts[-1] - px(3), pts[-2] + px(3), pts[-1] + px(3),
                             fill=color, outline=CARD, width=px(1))


class Pill(tk.Label):
    """Flat button made from a Label (tk.Button can't be styled this way on Windows)."""

    def __init__(self, parent, text, command, bg=TRACK, fg=FG, hover=HOVER, font=FONT_BOLD, **kw):
        super().__init__(parent, text=text, bg=bg, fg=fg, font=font, cursor="hand2",
                         padx=kw.pop("padx", px(10)), pady=kw.pop("pady", px(4)), **kw)
        self.base = self.active_bg = bg
        self.hover, self.fg = hover, fg
        self.enabled = True
        self.command = command
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=self.hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self.base))
        self.bind("<Button-1>", lambda e: self.enabled and self.command and self.command())

    def set_enabled(self, enabled):
        if enabled == self.enabled:
            return
        self.enabled = enabled
        if enabled:
            self.base = self.active_bg
        else:
            self.active_bg, self.base = self.base, TRACK   # disabled buttons go neutral grey
        self.configure(bg=self.base, fg=self.fg if enabled else DIM,
                       cursor="hand2" if enabled else "arrow")


def mini_button(parent, text):
    """Tiny square button for the port tiles."""
    b = tk.Label(parent, text=text, bg=TRACK, fg=FG, font=("Segoe UI", 8), width=2, cursor="hand2")
    b.bind("<Enter>", lambda e: b.configure(bg=HOVER) if b.cget("bg") == TRACK else None)
    b.bind("<Leave>", lambda e: b.configure(bg=TRACK) if b.cget("bg") == HOVER else None)
    return b


def fmt_uptime(seconds):
    s = int(float(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d {h}h {m:02d}m" if d else f"{h}h {m:02d}m"


def short_mode(mode):
    """COL-REFLECT -> REFLECT, US-DIST-CM -> DIST-CM (the prefix repeats the sensor name)."""
    return mode.split("-", 1)[1] if "-" in mode else mode


class PortTile(tk.Frame):
    """One port: badge, device name, big reading, a small visual, and controls."""

    def __init__(self, parent, app, port):
        super().__init__(parent, bg=TILE, padx=px(8), pady=px(6))
        self.port = port
        self.is_out = port.startswith("out")
        self.badge = tk.Label(self, text=port[-1], bg=LINE, fg=FG, font=FONT_BOLD, width=2)
        self.badge.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, px(8)), pady=(px(2), 0))
        self.name = tk.Label(self, text="empty", bg=TILE, fg=DIM, font=FONT_SMALL, anchor="w")
        self.name.grid(row=0, column=1, sticky="w")
        self.value = tk.Label(self, text="", bg=TILE, fg=FG, font=FONT_NUM_BIG, anchor="w")
        self.value.grid(row=1, column=1, sticky="w")
        d = px(30)
        self.visual = tk.Canvas(self, width=d, height=d, bg=TILE, highlightthickness=0)
        self.visual.grid(row=0, column=2, rowspan=2, padx=(px(4), 0))
        self.visual.grid_remove()
        self.controls = tk.Frame(self, bg=TILE)
        self.controls.grid(row=0, column=3, rowspan=2, sticky="e", padx=(px(6), 0))
        if self.is_out:
            for text, direction in (("◀", -1), ("▶", 1)):
                b = mini_button(self.controls, text)
                b.pack(side="left", padx=(px(1), 0))
                b.bind("<ButtonPress-1>", lambda e, d=direction, w=b: app._jog_start(port, d, w))
                b.bind("<ButtonRelease-1>", lambda e, w=b: app._jog_stop(w))
        else:
            b = mini_button(self.controls, "⟳")
            b.pack()
            b.bind("<Button-1>", lambda e: app._next_mode(port))
            b.bind("<Enter>", lambda e: app._show_modes(port), add="+")
        self.controls.grid_remove()
        self.columnconfigure(1, weight=1)

    def _set(self, badge_bg, name, name_fg, value="", value_fg=FG, controls=False):
        self.badge.configure(bg=badge_bg, fg=BG if badge_bg not in (LINE,) else FG)
        self.name.configure(text=name, fg=name_fg)
        self.value.configure(text=value, fg=value_fg)
        self.controls.grid() if controls else self.controls.grid_remove()

    def show_motor(self, m):
        moving = m["speed"] != 0
        name = DEVICE_NAMES.get(m["driver"], m["driver"])
        self._set(ACCENT, f"{name} · {m['speed']}°/s", MUTED,
                  f"{m['position']}°", GOOD if moving else FG, controls=True)
        self._dial(m["position"], GOOD if moving else ACCENT)

    def show_sensor(self, s):
        name = DEVICE_NAMES.get(s["driver"], s["driver"])
        vals = s["values"]
        visual = None
        if s["mode"] == "COL-COLOR" and vals:
            label, swatch = COLOR_NAMES.get(int(vals[0]), ("?", DIM))
            text, visual = label, ("swatch", swatch)
        elif s["driver"] in ("lego-ev3-touch", "lego-nxt-touch") and vals:
            text = "pressed" if vals[0] else "released"
        else:
            text = " ".join(f"{v:g}" for v in vals[:3]) + (f" {s['units']}" if s["units"] else "")
            if s["units"] == "pct" and len(vals) == 1:
                visual = ("ring", vals[0] / 100)
        pressed = text == "pressed"
        self._set(VIOLET, f"{name} · {short_mode(s['mode'])}", MUTED, text,
                  GOOD if pressed else FG, controls=len(s["modes"]) > 1)
        if visual and visual[0] == "swatch":
            self._swatch(visual[1])
        elif visual:
            self._ring(visual[1])
        else:
            self.visual.grid_remove()

    def show_empty(self, status):
        if status == "error":
            self._set(WARN, "not recognized", WARN, "check cable", WARN)
        else:
            self._set(LINE, "empty", DIM)
        self.visual.grid_remove()

    # little pictures in the tile
    def _canvas(self):
        c = self.visual
        c.grid()
        c.delete("all")
        return c, int(c.cget("width"))

    def _dial(self, position, color):
        c, d = self._canvas()
        m = px(3)
        c.create_oval(m, m, d - m, d - m, outline=LINE, width=px(2))
        c.create_line(d / 2, m, d / 2, m + px(3), fill=MUTED, width=px(2))   # 0° mark
        a = math.radians(position % 360)
        r = d / 2 - m - px(2)
        c.create_line(d / 2, d / 2, d / 2 + r * math.sin(a), d / 2 - r * math.cos(a),
                      fill=color, width=px(2), capstyle="round")
        c.create_oval(d / 2 - px(2), d / 2 - px(2), d / 2 + px(2), d / 2 + px(2), fill=color, outline="")

    def _ring(self, fraction):
        c, d = self._canvas()
        m = px(4)
        c.create_oval(m, m, d - m, d - m, outline=LINE, width=px(3))
        if fraction > 0.005:
            c.create_arc(m, m, d - m, d - m, start=90, extent=-359.9 * min(1, fraction),
                         style="arc", outline=VIOLET, width=px(3))

    def _swatch(self, color):
        c, d = self._canvas()
        m = px(5)
        c.create_oval(m, m, d - m, d - m, fill=color, outline=LINE, width=px(2))


class Widget(tk.Tk):
    def __init__(self):
        super().__init__()
        global SCALE
        if sys.platform == "darwin":   # macOS Tk assumes 72 dpi, which draws everything ~25% small
            self.tk.call("tk", "scaling", 96 / 72)
        SCALE = self.winfo_fpixels("1i") / 96
        self.title("EV3 Status")
        # Borderless on Windows only: on macOS/Linux such windows can't take keyboard focus
        # (the Say box would be dead) and can't be moved by the window manager.
        self.overrideredirect(sys.platform == "win32")
        self.attributes("-topmost", True)
        self.configure(bg=BG, highlightthickness=1, highlightbackground=LINE)
        self.topmost = tk.BooleanVar(value=True)
        self.live = tk.BooleanVar(value=False)
        self.queue = queue.Queue()
        self.busy = False
        self.action_msg = ("", 0.0)
        self.data = {"motors": {}, "sensors": {}}
        self.jog = None   # (port, after-id) while a motor button is held
        self.sounds = []     # full paths of the WAVs on the brick
        self.sound = None    # the picked one
        self.volume = None   # % once read from the brick; ours to change after that
        self.home = None     # the robot user's home folder on the brick
        self.dot_color = MUTED
        self.pulse_t = 0.0

        self.poller = Poller(self.queue)
        self.cards = {}   # title -> (frame, head, title label), for folding

        self._build_header()
        self._build_gauges()
        self._build_activity()
        self._build_stats()
        self._build_program()
        self._build_sound()
        self._build_ports()
        self._build_footer()
        self._build_menu()
        self._fit_screen()

        self.update_idletasks()
        x = self.winfo_screenwidth() - self.winfo_reqwidth() - px(24)
        self.geometry(f"+{x}+{px(48)}")
        self._round_corners()

        self.poller.start()
        self.after(100, self._drain_queue)
        self.after(100, self._pulse)

    def _round_corners(self):
        """Windows 11 rounded corners for this borderless window (ignored elsewhere)."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            pref = ctypes.c_int(2)   # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

    # ---------- layout ----------
    def _card(self, title=None, right=None):
        frame = tk.Frame(self, bg=CARD, padx=px(12), pady=px(9))
        frame.pack(fill="x", padx=px(10), pady=(0, px(8)))
        if title:
            head = tk.Frame(frame, bg=CARD)
            head.pack(fill="x", pady=(0, px(6)))
            label = tk.Label(head, text="▾  " + title.upper(), bg=CARD, fg=MUTED, font=FONT_CAPS,
                             cursor="hand2")
            label.pack(side="left")
            label.bind("<Button-1>", lambda e: self._fold(frame, head, label))
            self.cards[title] = (frame, head, label)
            if right:
                right(head)
        return frame

    def _fold(self, frame, head, label):
        """Click a card's title to hide or show everything under it (for small screens)."""
        folded = getattr(frame, "folded", None)
        if folded:
            for w, info in folded:
                w.pack(**info)
            frame.folded = None
            head.pack_configure(pady=(0, px(6)))
        else:
            frame.folded = [(w, w.pack_info()) for w in frame.winfo_children()
                            if w is not head and w.winfo_manager() == "pack"]
            for w, _ in frame.folded:
                w.pack_forget()
            head.pack_configure(pady=0)
        title = label.cget("text")[3:]
        label.configure(text=("▾  " if folded else "▸  ") + title)

    def _fit_screen(self):
        """Fold the least-needed cards while the window is taller than the screen."""
        for title in ("Activity", "System"):
            self.update_idletasks()
            if self.winfo_reqheight() <= self.winfo_screenheight() - px(96):
                return
            self._fold(*self.cards[title])

    def _build_header(self):
        self.strip = tk.Canvas(self, height=px(3), bg=BG, highlightthickness=0)
        self.strip.pack(fill="x")
        self.strip.bind("<Configure>", self._draw_strip)

        header = tk.Frame(self, bg=BG, padx=px(12), pady=px(10))
        header.pack(fill="x")
        d = px(12)
        self.dot = tk.Canvas(header, width=d, height=d, bg=BG, highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_item = self.dot.create_oval(px(2), px(2), d - px(2), d - px(2), fill=MUTED, outline="")
        self.title_label = tk.Label(header, text="EV3", bg=BG, fg=FG, font=FONT_TITLE)
        self.title_label.pack(side="left", padx=(px(6), 0))
        self.host_label = tk.Label(header, text="", bg=BG, fg=MUTED, font=FONT)
        self.host_label.pack(side="left", padx=(px(6), 0))
        close = tk.Label(header, text="✕", bg=BG, fg=MUTED, font=FONT, cursor="hand2")
        close.pack(side="right")
        close.bind("<Enter>", lambda e: close.configure(fg=BAD))
        close.bind("<Leave>", lambda e: close.configure(fg=MUTED))
        close.bind("<Button-1>", lambda e: self.destroy())
        self.mode_toggle = Pill(header, "", lambda: self._set_live(not self.live.get()),
                                font=("Segoe UI Semibold", 8), padx=px(9), pady=px(1))
        self.mode_toggle.pack(side="right", padx=(0, px(10)))
        self._set_live(False)
        for w in (header, self.dot, self.title_label, self.host_label):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._drag)

    def _draw_strip(self, event):
        c, w = self.strip, event.width
        c.delete("all")
        n = 48
        for i in range(n):
            c.create_rectangle(i * w / n, 0, (i + 1) * w / n + 1, px(3), outline="",
                               fill=blend(ACCENT, VIOLET, i / (n - 1)))

    def _build_gauges(self):
        f = tk.Frame(self, bg=CARD, padx=px(6), pady=px(8))
        f.pack(fill="x", padx=px(10), pady=(0, px(8)))
        self.g_bat = Gauge(f, "Battery")
        self.g_cpu = Gauge(f, "CPU")
        self.g_mem = Gauge(f, "Memory")
        for i, g in enumerate((self.g_bat, self.g_cpu, self.g_mem)):
            g.grid(row=0, column=i, padx=px(4))
            f.columnconfigure(i, weight=1)

    def _build_activity(self):
        def legend(head):
            self.act_mem = tk.Label(head, text="", bg=CARD, fg=VIOLET, font=FONT_SMALL)
            self.act_mem.pack(side="right")
            self.act_cpu = tk.Label(head, text="", bg=CARD, fg=ACCENT, font=FONT_SMALL)
            self.act_cpu.pack(side="right", padx=(0, px(10)))
        f = self._card("Activity", legend)
        self.spark = Spark(f)
        self.spark.pack(fill="x")
        self.s_cpu = self.spark.add(ACCENT)
        self.s_mem = self.spark.add(VIOLET)

    def _build_stats(self):
        f = self._card("System")
        grid = tk.Frame(f, bg=CARD)
        grid.pack(fill="x")
        self.stats = {}
        items = ["Voltage", "Current", "Cache", "Swap (zram)", "Storage", "Uptime", "IP", "OS"]
        for i, name in enumerate(items):
            cell = tk.Frame(grid, bg=CARD)
            cell.grid(row=i // 2, column=i % 2, sticky="w", pady=(0, px(6)))
            tk.Label(cell, text=name.upper(), bg=CARD, fg=DIM, font=FONT_CAPS).pack(anchor="w")
            v = tk.Label(cell, text="–", bg=CARD, fg=FG, font=FONT_NUM)
            v.pack(anchor="w")
            self.stats[name] = v
        grid.columnconfigure((0, 1), weight=1, uniform="stats")

        buttons = tk.Frame(f, bg=CARD)
        buttons.pack(fill="x", pady=(px(4), 0))
        self.buttons = {}
        for name, text, tip in (("clear", "Clear cache", "Empty the file cache"),
                                ("optimize", "Optimize", "Clear cache and defragment free RAM")):
            b = Pill(buttons, text, lambda n=name: self._do_action(n))
            b.pack(side="left", expand=True, fill="x", padx=(0, px(6)) if name == "clear" else 0)
            b.bind("<Enter>", lambda e, t=tip: self._hint(t), add="+")
            self.buttons[name] = b

    def _build_program(self):
        f = tk.Frame(self, bg=CARD, padx=px(12), pady=px(8))
        f.pack(fill="x", padx=px(10), pady=(0, px(8)))
        self.prog_icon = tk.Label(f, text="■", bg=CARD, fg=DIM, font=("Segoe UI", 10))
        self.prog_icon.pack(side="left")
        box = tk.Frame(f, bg=CARD)
        box.pack(side="left", padx=(px(8), 0))
        tk.Label(box, text="PROGRAM", bg=CARD, fg=DIM, font=FONT_CAPS).pack(anchor="w")
        self.program = tk.Label(box, text="none", bg=CARD, fg=MUTED, font=FONT_BOLD)
        self.program.pack(anchor="w")
        stop = Pill(f, "Stop", lambda: self._do_action("stop"), bg=blend(BAD, CARD, 0.55),
                    hover=blend(BAD, CARD, 0.35))
        stop.pack(side="right")
        stop.bind("<Enter>", lambda e: self._hint("Stop the running robot program"), add="+")
        self.buttons["stop"] = stop

    def _build_sound(self):
        def volume(head):
            plus = mini_button(head, "+")
            plus.pack(side="right")
            self.vol_label = tk.Label(head, text="–", bg=CARD, fg=FG, font=FONT_NUM, width=4)
            self.vol_label.pack(side="right")
            minus = mini_button(head, "−")
            minus.pack(side="right")
            tk.Label(head, text="VOLUME", bg=CARD, fg=DIM, font=FONT_CAPS).pack(side="right", padx=(0, px(6)))
            minus.bind("<Button-1>", lambda e: self._change_volume(-ev3_sound.VOLUME_STEP))
            plus.bind("<Button-1>", lambda e: self._change_volume(ev3_sound.VOLUME_STEP))
        f = self._card("Sound", volume)

        row = tk.Frame(f, bg=CARD)
        row.pack(fill="x")
        self.sound_pick = Pill(row, "Pick a sound  ▾", self._sound_menu, bg=TILE, font=FONT_SMALL, anchor="w")
        self.sound_pick.pack(side="left", fill="x", expand=True)
        play = Pill(row, "▶ Play", self._play_sound, bg=blend(GOOD, CARD, 0.6),
                    hover=blend(GOOD, CARD, 0.4), font=FONT_SMALL)
        play.pack(side="left", padx=(px(6), 0))
        Pill(row, "■", self._stop_sound, font=FONT_SMALL).pack(side="left", padx=(px(4), 0))

        row = tk.Frame(f, bg=CARD)
        row.pack(fill="x", pady=(px(6), 0))
        self.say_text = tk.StringVar()
        entry = tk.Entry(row, textvariable=self.say_text, bg=TILE, fg=FG, insertbackground=FG,
                         relief="flat", highlightthickness=0, font=FONT_SMALL)
        entry.pack(side="left", fill="x", expand=True, ipady=px(4))
        entry.bind("<Return>", lambda e: self._say())
        entry.bind("<Enter>", lambda e: self._hint("Type something for the robot to say, then Enter"))
        Pill(row, "Say", self._say, font=FONT_SMALL).pack(side="left", padx=(px(6), 0))
        upload = Pill(row, "Upload…", self._upload_sound, font=FONT_SMALL)
        upload.pack(side="left", padx=(px(4), 0))
        upload.bind("<Enter>", lambda e: self._hint("Copy a .wav file from this computer to the brick"), add="+")

    def _build_ports(self):
        f = self._card("Ports")
        grid = tk.Frame(f, bg=CARD)
        grid.pack(fill="x")
        self.tiles = {}
        for i, port in enumerate(PORTS):
            col, row = (0, i) if i < 4 else (1, i - 4)
            tile = PortTile(grid, self, port)
            tile.grid(row=row, column=col, sticky="nsew", padx=(0, px(6)) if col == 0 else 0,
                      pady=(0, px(6)))
            self.tiles[port] = tile
        grid.columnconfigure((0, 1), weight=1, uniform="ports", minsize=px(168))

    def _build_footer(self):
        self.footer = tk.Label(self, text="", bg=BG, fg=MUTED, font=FONT_SMALL,
                               anchor="w", padx=px(12), wraplength=px(340), justify="left")
        self.footer.pack(fill="x", pady=(0, px(10)))

    def _build_menu(self):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Refresh now", command=self.poller.wake.set)
        menu.add_checkbutton(label="Live mode", variable=self.live,
                             command=lambda: self._set_live(self.live.get()))
        menu.add_checkbutton(label="Always on top", variable=self.topmost,
                             command=lambda: self.attributes("-topmost", self.topmost.get()))
        menu.add_separator()
        menu.add_command(label="Close", command=self.destroy)
        popup = lambda e: menu.tk_popup(e.x_root, e.y_root)
        if sys.platform == "darwin":   # macOS Tk: right-click is Button-2, or Control-click
            self.bind_all("<Button-2>", popup)
            self.bind_all("<Control-Button-1>", popup)
        else:
            self.bind_all("<Button-3>", popup)

    # ---------- status dot ----------
    def _set_dot(self, color):
        self.dot_color = color

    def _pulse(self):
        """The status dot breathes while LIVE and connected; otherwise it's steady."""
        color = self.dot_color
        if self.live.get() and color == GOOD:
            self.pulse_t += 0.12
            color = blend(GOOD, BG, 0.55 * (0.5 + 0.5 * math.sin(self.pulse_t)))
        self.dot.itemconfigure(self.dot_item, fill=color)
        self.after(50, self._pulse)

    # ---------- motors & sensors ----------
    def _jog_start(self, port, direction, button):
        motor = self.data["motors"].get(port)
        if motor is None:
            return
        button.configure(bg=ACCENT)
        path = motor["path"]
        cmd = (f"echo {direction * MOTOR_SPEED} > {path}/speed_sp; "
               f"echo {JOG_PULSE_MS} > {path}/time_sp; echo run-timed > {path}/command")

        def pulse():
            self.poller.send(cmd)
            self.jog = (path, self.after(JOG_RENEW_MS, pulse))

        pulse()
        self.poller.interval = LIVE_REFRESH_SECONDS
        self.poller.wake.set()

    def _jog_stop(self, button):
        button.configure(bg=TRACK)
        if self.jog is None:
            return
        path, after_id = self.jog
        self.after_cancel(after_id)
        self.jog = None
        self.poller.send(f"echo stop > {path}/command")
        # Keep refreshing fast for a moment so the final position shows up.
        self.after(1500, lambda: setattr(self.poller, "interval", self._interval())
                   if self.jog is None else None)

    # ---------- refresh mode ----------
    def _interval(self):
        return LIVE_REFRESH_SECONDS if self.live.get() else AUTO_REFRESH_SECONDS

    def _set_live(self, live):
        """LIVE refreshes ~3x a second; AUTO every few seconds to keep the brick idle."""
        self.live.set(live)
        if live:
            bg, fg, text = GOOD, BG, "● LIVE"
        else:
            bg, fg, text = TRACK, FG, f"AUTO · {AUTO_REFRESH_SECONDS:g}s"
        self.mode_toggle.base, self.mode_toggle.fg = bg, fg
        self.mode_toggle.hover = blend(bg, "#ffffff", 0.15)
        self.mode_toggle.configure(text=text, bg=bg, fg=fg)
        if self.jog is None:
            self.poller.interval = self._interval()
        self.poller.wake.set()

    def _next_mode(self, port):
        sensor = self.data["sensors"].get(port)
        if not sensor or not sensor["modes"]:
            return
        modes = sensor["modes"]
        nxt = modes[(modes.index(sensor["mode"]) + 1) % len(modes)] if sensor["mode"] in modes else modes[0]
        self.poller.send(f"echo {nxt} > {sensor['path']}/mode")
        self._hint(f"Port {port[-1]}: switching to {nxt}")
        self.poller.wake.set()

    def _show_modes(self, port):
        sensor = self.data["sensors"].get(port)
        if sensor:
            self._hint("Click to cycle modes: " + " · ".join(sensor["modes"]))

    def _hint(self, text, seconds=3):
        """Show a message in the footer that survives the next few refreshes."""
        self.footer.configure(text=text)
        self.action_msg = (text, time.time() + seconds)

    # ---------- sound ----------
    def _sound_menu(self):
        if not self.sounds:
            self._hint("No sounds yet: waiting for the brick")
            return
        menu = tk.Menu(self, tearoff=0)
        for group, paths in ev3_sound.grouped(self.sounds):
            sub = tk.Menu(menu, tearoff=0)
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
        self.poller.send(ev3_sound.play(self.sound))
        self._hint(f"Playing {ev3_sound.name(self.sound)}")

    def _stop_sound(self):
        self.poller.send(ev3_sound.STOP)

    def _say(self):
        text = self.say_text.get().strip()
        if not text:
            self._hint("Type something for the robot to say first")
            return
        if self.home:   # keep it, so it joins "my sounds" and ▶ Play / P replays it
            cmd, path = ev3_sound.say_and_keep(text, self.home)
            self.poller.send(cmd)
            if path not in self.sounds:
                self.sounds.append(path)
            self._pick_sound(path, play=False)
            self._hint(f"Saying “{text}” · saved to my sounds")
        else:
            self.poller.send(ev3_sound.say(text))
            self._hint(f"Saying “{text}”")

    def _change_volume(self, delta):
        if self.volume is None:
            return
        self.volume = max(0, min(100, self.volume + delta))
        self.vol_label.configure(text=f"{self.volume}%")
        self.poller.send(ev3_sound.set_volume(self.volume))

    def _upload_sound(self):
        path = filedialog.askopenfilename(parent=self, title="Upload a sound to the EV3",
                                          filetypes=[("WAV sound", "*.wav"), ("All files", "*.*")])
        if not path:
            return
        self._hint(f"Uploading {os.path.basename(path)}…", 120)
        self.poller.upload(path)

    def _uploaded(self, path, message):
        if path:
            if path not in self.sounds:
                self.sounds.append(path)
            self._pick_sound(path, play=False)   # ready for ▶ Play
        self._hint(message, 6)

    # ---------- memory buttons ----------
    def _do_action(self, name):
        if name == "stop" and not messagebox.askyesno(
                "Stop program", f"Stop {self.program.cget('text')} on the EV3?", parent=self):
            return
        for b in self.buttons.values():
            b.set_enabled(False)
        self.busy = True
        self.footer.configure(text={"clear": "Clearing cache…", "optimize": "Optimizing memory…",
                                    "stop": "Stopping program…"}[name])
        self.poller.request(name)

    def _action_done(self, name, result):
        self.busy = False
        for b in self.buttons.values():
            b.set_enabled(True)
        if result.get("rc", "1") != "0" and name != "stop":
            msg = "Failed: the brick refused sudo (check the password in ev3_config.json)"
        elif name == "stop":
            msg = "Program stopped" if result.get("stopped") else "No program was running"
        else:
            freed = max(0, int(result.get("freed", "0"))) // 1024
            msg = f"Freed {freed} MB of RAM" + (" and compacted memory" if name == "optimize" else "")
        self.action_msg = (msg, time.time() + 6)

    # ---------- dragging ----------
    def _start_drag(self, event):
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag(self, event):
        dx, dy = self._drag_offset
        self.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    # ---------- updates ----------
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "data":
                    self._show(payload)
                elif kind == "action":
                    self._action_done(*payload)
                elif kind == "uploaded":
                    self._uploaded(*payload)
                elif kind == "status":
                    self._set_dot(WARN)
                    self.footer.configure(text=payload)
                else:
                    if self.busy:  # the connection dropped mid-action; unlock the buttons
                        self.busy = False
                        for b in self.buttons.values():
                            b.set_enabled(True)
                    self._set_dot(BAD)
                    self.footer.configure(text=f"Disconnected: {payload}")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _show(self, d):
        self.data = d
        self._set_dot(GOOD)
        self.host_label.configure(text=d.get("host", ""))

        volt, curr = (int(x) for x in d["bat"].split())
        vmin, vmax = BATTERY_RANGE.get(d.get("bat_type"), AA_RANGE)
        pct = max(0.0, min(1.0, (volt / 1e6 - vmin) / (vmax - vmin)))
        color = GOOD if pct > 0.5 else WARN if pct > 0.2 else BAD
        self.g_bat.set(pct, f"{pct:.0%}", f"{volt / 1e6:.2f} V", color)

        load1 = float(d["load"].split()[0])
        self.g_cpu.set(load1, f"{load1:.2f}", "load avg", WARN if load1 > 0.8 else ACCENT)

        mem_total, mem_avail, cache, swap_total, swap_used = (int(x) for x in d["mem"].split())
        used = mem_total - mem_avail
        self.g_mem.set(used / mem_total, f"{used / mem_total:.0%}",
                       f"{used // 1024} / {mem_total // 1024} MB",
                       WARN if used / mem_total > 0.8 else VIOLET)

        self.spark.push(self.s_cpu, load1)
        self.spark.push(self.s_mem, used / mem_total)
        self.spark.redraw()
        self.act_cpu.configure(text=f"● CPU {load1:.2f}")
        self.act_mem.configure(text=f"● RAM {used / mem_total:.0%}")

        st = self.stats
        st["Voltage"].configure(text=f"{volt / 1e6:.2f} V")
        st["Current"].configure(text=f"{curr / 1000:.0f} mA")
        st["Cache"].configure(text=f"{cache // 1024} MB")
        st["Swap (zram)"].configure(text=f"{swap_used // 1024} / {swap_total // 1024} MB")
        if "disk" in d:
            disk_total, disk_used = (int(x) for x in d["disk"].split())
            st["Storage"].configure(text=f"{disk_used / 1048576:.1f} / {disk_total / 1048576:.0f} GB")
        st["Uptime"].configure(text=fmt_uptime(d["uptime"]))
        st["IP"].configure(text=(d.get("ip") or "–").split()[0])
        st["OS"].configure(text=f"{d.get('os', '–').replace('ev3dev-', '')} · {d.get('kernel', '').split('-')[0]}")

        if d.get("sounds"):
            sounds = d["sounds"].split()
            self.sounds = sounds + [s for s in self.sounds if s not in sounds]   # keep fresh uploads
        self.home = d.get("home") or self.home
        if self.volume is None and d.get("vol", "").isdigit():
            self.volume = int(d["vol"])
            self.vol_label.configure(text=f"{self.volume}%")

        prog = d.get("program") or ""
        self.program.configure(text="/".join(prog.split("/")[-2:]) if prog else "none",
                               fg=FG if prog else MUTED)
        self.prog_icon.configure(text="▶" if prog else "■", fg=GOOD if prog else DIM)
        if not self.busy:
            self.buttons["stop"].set_enabled(bool(prog))

        for port, tile in self.tiles.items():
            if port in d["motors"]:
                tile.show_motor(d["motors"][port])
            elif port in d["sensors"]:
                tile.show_sensor(d["sensors"][port])
            else:
                tile.show_empty(d["ports"].get(port, ""))

        msg, until = self.action_msg
        if self.busy:
            pass
        elif time.time() < until:
            self.footer.configure(text=msg)
        else:
            mode = "live" if self.live.get() else f"every {AUTO_REFRESH_SECONDS:g}s"
            self.footer.configure(text=f"Updated {time.strftime('%H:%M:%S')} · {mode} · {HOST}")
        self._keep_on_screen()

    def _keep_on_screen(self):
        """The window grows as values fill in; slide it left so it never runs off-screen."""
        self.update_idletasks()
        overflow = self.winfo_x() + self.winfo_reqwidth() - self.winfo_screenwidth()
        if overflow > 0:
            self.geometry(f"+{self.winfo_x() - overflow - px(24)}+{self.winfo_y()}")


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # crisp text on scaled displays
    except Exception:
        pass
    Widget().mainloop()
