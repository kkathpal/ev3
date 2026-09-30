"""EV3 RC for phones — drive the robot from any phone browser on your Wi-Fi.

This PC keeps the connection to the bricks (like ev3_drive.pyw) and serves a touch
controller page to phones on the same Wi-Fi. It drives exactly like the desktop
app: same gears, Turbo, and calibration (read from ev3_drive_settings.json).

Each phone picks its own brick: phones on different bricks drive them independently,
and phones on the same brick share it (one drives at a time). This PC's own brick (from
ev3_config.json) uses the desktop app's settings; other bricks get their own, saved in
the settings file's "bricks" section.

Run:   python ev3_phone.py               (brick address from ev3_config.json)
       python ev3_phone.py 192.168.0.1   (brick at a specific address)
       python ev3_phone.py --port 8090   (use this port)
It uses a random free port from 8000-8999 (unless --port picks one) and prints
the http://<this PC>:<port> address to open on your phone.

Safety: the phone re-sends the joystick position (or held keys) every ~100 ms. Each command only runs
the motors briefly (Turbo is covered by the brick-side watchdog), and this server
also stops the robot if a phone goes quiet for PHONE_TIMEOUT, so lifting your
finger, locking the phone or losing Wi-Fi stops the robot.
"""
import json
import math
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))


def take_port_arg():
    """Remove `--port N` from the command line and return N (or None). It must go before
    ev3_drive loads, since ev3_config reads the first argument as the brick's address."""
    args = sys.argv
    if "--port" not in args:
        return None
    i = args.index("--port")
    if i + 1 < len(args) and args[i + 1].isdigit():
        port = int(args[i + 1])
        del args[i:i + 2]
        return port
    sys.exit("Use:  python ev3_phone.py [brick-address] --port 8090")


PORT_ARG = take_port_arg()
rc = SourceFileLoader("ev3_drive", os.path.join(HERE, "ev3_drive.pyw")).load_module()
import ev3_find   # noqa: E402  (next to this file, like ev3_drive)
from ev3_find import lan_addresses   # noqa: E402

PORT_RANGE = (8000, 8999)   # each run uses a random free port from here (--port N picks one)
PAGE = os.path.join(HERE, "ev3_phone.html")
PHONE_TIMEOUT = 0.35   # stop if the driving phone sends nothing for this long (s)
DRIVER_IDLE = 1.0      # one phone drives at a time: another may take over once it's sent nothing for this long (s)
DIRECTIONS = {"up", "down", "left", "right"}
REAP_AFTER = 120       # let go of a brick (motors stopped) once no phone has used it for this long (s)


def parse_stick(value):
    """A /drive request's joystick position as (x, y), each clamped to -1..1, or None if it
    isn't one (json.loads accepts NaN and Infinity, which would reach the motors)."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
        return None
    return tuple(max(-1.0, min(1.0, float(v))) for v in value)


class Controller:
    """One brick: owns its connection and turns the joystick (or held keys) of the phones that
    picked it into drive pulses. The Hub keeps one per brick in use."""

    def __init__(self, host=None):
        self.host = host or rc.HOST
        self.brick = rc.Brick()
        self.brick.host = self.host
        self.stop_event = threading.Event()   # set by shutdown(): ends the monitor and safety threads
        self.lock = threading.Lock()
        self.settings, self.settings_mtime = {}, None
        self._reload_settings()
        self.mode = self.settings.get("mode", "Normal")
        if self.mode not in dict(rc.MODES):
            self.mode = "Normal"
        self.held = set()
        self.stick = None      # (x, y) while a thumb is on the joystick; wins over held keys
        self.moving = False
        self.last_command = 0.0
        self.status = "Connecting…"
        self.readings = {}
        self.known_ports = []   # the brick's motors when the pair was last checked
        self.trip_cm = self.top_speed = 0.0
        self.last_pos = None
        self.telemetry = {}
        self.driver = None          # the phone holding the controls (its page's random id)
        self.driver_seen = 0.0      # when it last sent input that moves the robot
        threading.Thread(target=self._monitor, daemon=True).start()
        threading.Thread(target=self._safety, daemon=True).start()

    # ----- settings: this PC's own brick shares the desktop app's; other bricks have their own -----
    def _own(self):
        """This PC's own brick (ev3_config.json), whose settings the desktop app uses too."""
        name = self.brick.name
        return self.host == rc.HOST or bool(name and f"{name}.local" == rc.HOST)

    def _key(self):
        """Where another brick's settings live in the file's "bricks" section: its own name
        (the same whether reached over Bluetooth or Wi-Fi), or its address until connected."""
        return self.brick.name or self.host

    def _reload_settings(self):
        """Pick up calibration changes made in ev3_drive.pyw (or another phone) while this runs."""
        try:
            mtime = os.path.getmtime(rc.SETTINGS_FILE)
        except OSError:
            mtime = None
        if mtime != self.settings_mtime:
            file = rc.load_settings()
            top = {k: v for k, v in file.items() if k != "bricks"}
            if self._own():
                self.settings = top
            else:   # start from this PC's settings (gear, acceleration...) until calibrated
                self.settings = {**top, **file.get("bricks", {}).get(self._key(), {})}
            self.settings_mtime = mtime

    def _write(self, changes):
        """Save setting changes for this brick (call with the lock held)."""
        if self._own():
            rc.save_settings({**self.settings, **changes})
        else:
            file = rc.load_settings()
            bricks = dict(file.get("bricks", {}))
            bricks[self._key()] = {**bricks.get(self._key(), {}), **changes}
            rc.save_settings({**file, "bricks": bricks})
        self.settings_mtime = None   # re-read now

    def _ports(self):
        s = self.settings
        return s.get("left", rc.DEFAULT_LEFT), s.get("right", rc.DEFAULT_RIGHT)

    def _check_ports(self):
        """When the brick's motors change, pick the pair like the desktop does: the saved one if
        it's plugged in, otherwise the motors that are (saved, so both apps agree)."""
        ports = sorted(self.brick.paths)
        if ports == self.known_ports:
            return
        self.known_ports = ports
        with self.lock:
            self._reload_settings()
            left, right = self._ports()
            picked = rc.pick_motors(left, right, ports)
            if picked != (left, right):
                self._save({"left": picked[0], "right": picked[1]})   # stops first

    def _inverts(self):
        legacy = self.settings.get("reverse", False)
        return (self.settings.get("invert_left", legacy), self.settings.get("invert_right", legacy))

    # ----- driving -----
    def _claim(self, client, moving):
        """One phone drives at a time, so two phones on this page can't fight over the robot.
        True if `client` may drive now: nobody holds the controls (none yet, or the holder has
        sent nothing that moves the robot for DRIVER_IDLE), or it holds them. Moving input
        (`moving`) claims them. Call with the lock held."""
        now = time.monotonic()
        if self.driver not in (None, client) and now - self.driver_seen < DRIVER_IDLE:
            return False
        if moving:
            self.driver, self.driver_seen = client, now
        return True

    def control(self, client):
        """For the page: "you" (holds the controls), "other" (another phone does) or "free"."""
        if self.driver is None or time.monotonic() - self.driver_seen >= DRIVER_IDLE:
            return "free"
        return "you" if self.driver == client else "other"

    def take_over(self, client):
        """Take the controls from another phone on purpose: stop the robot, then hold them."""
        with self.lock:
            self.stop()
            self.driver, self.driver_seen = client, time.monotonic()

    def request_stop(self, client, explicit=False):
        """The STOP button (explicit) stops the robot from any phone, for safety. A phone just
        going away (locked, switched app) only stops it if that phone is the one driving."""
        with self.lock:
            if explicit or self._claim(client, False):
                self.stop()

    def drive(self, held, mode=None, stick=None, client=None):
        with self.lock:
            moving = parse_stick(stick) is not None or (
                isinstance(held, list) and any(d in DIRECTIONS for d in held))
            if not self._claim(client, moving):
                return False   # another phone is driving: ignore this one
            if mode in dict(rc.MODES) and mode != self.mode:
                self.set_mode(mode)
            self.stick = parse_stick(stick)
            self.held = {d for d in held if d in DIRECTIONS} if isinstance(held, list) else set()
            self.last_command = time.monotonic()
            self._send()
            return True

    def _send(self):
        self._reload_settings()
        inv_l, inv_r = self._inverts()
        trim = self.settings.get("trim", 0)
        if self.stick:
            left, right = rc.stick_commands(*self.stick, self.mode, trim)
        else:
            left, right = rc.wheel_commands(self.held, self.mode, trim)
        lp_name, rp_name = self._ports()
        lp, rp = self.brick.paths.get(lp_name), self.brick.paths.get(rp_name)
        if (left or right) and self.brick.connected and lp and rp and lp != rp:
            rc.send_drive(self.brick, lp, rp, left, right, self.mode, inv_l, inv_r,
                          rc.ramp_seconds(self.settings))   # the desktop app's Acceleration setting
            self.moving = True
        elif self.moving or not (self.held or self.stick):
            self.stop(released=True)

    def stop(self, released=False):
        self.held = set()
        self.stick = None
        if self.brick.connected:
            self.brick.stop(released)
        self.moving = False

    def set_mode(self, mode):
        self.mode = mode
        self._reload_settings()
        self._write({"mode": mode})   # this brick opens in the same gear next time

    def reset_trip(self):
        self.trip_cm = self.top_speed = 0.0

    # ----- Setup · Calibrate from the phone (same as the desktop's Setup card) -----
    def cal_test(self, arrow, client=None):
        """Calibrate by driving: run the picked motors in CAL_PATTERNS[arrow] (no Invert, slow
        TEST_SPEED timed pulses) while the phone keeps re-sending it; _safety stops it after."""
        with self.lock:
            if not self._claim(client, arrow in rc.CAL_PATTERNS):
                return   # another phone is driving
            self.held = set()
            self.stick = None
            self.last_command = time.monotonic()
            self._reload_settings()
            lp_name, rp_name = self._ports()
            lp, rp = self.brick.paths.get(lp_name), self.brick.paths.get(rp_name)
            if arrow in rc.CAL_PATTERNS and self.brick.connected and lp and rp and lp != rp:
                a, b = rc.CAL_PATTERNS[arrow]
                self.brick.drive(lp, rp, a * rc.TEST_SPEED, b * rc.TEST_SPEED)
                self.moving = True
            elif self.moving:
                self.stop(released=True)

    def _save(self, changes):
        """Stop, then save setting changes (call with the lock held)."""
        self.stop()
        self._reload_settings()
        self._write(changes)

    def update_setup(self, data):
        """Motors, Invert, Drift fix or Acceleration changed on the phone."""
        changes = {}
        for key in ("left", "right"):
            if data.get(key) in self.brick.paths:
                changes[key] = data[key]
        for key in ("invert_left", "invert_right"):
            if isinstance(data.get(key), bool):
                changes[key] = data[key]
        if isinstance(data.get("trim"), (int, float)) and not isinstance(data.get("trim"), bool):
            changes["trim"] = int(max(-rc.TRIM_RANGE, min(rc.TRIM_RANGE, data["trim"])))
        if data.get("accel") in dict(rc.ACCELERATIONS):
            changes["accel"] = data["accel"]
        if changes:
            with self.lock:
                self._save(changes)

    def save_calibration(self, choice):
        """Calibrate by driving's answers → Swap/Invert, like the desktop. Returns an error or ""."""
        result, reason = rc.cal_result(choice if isinstance(choice, dict) else {})
        if not result:
            return reason
        swap, invert_left, invert_right = result
        with self.lock:
            left, right = self._ports()
            changes = {"invert_left": invert_left, "invert_right": invert_right}
            if swap:
                changes.update(left=right, right=left)
            self._save(changes)
        return ""

    def _setup_state(self):
        inv_l, inv_r = self._inverts()
        left, right = self._ports()
        accel = self.settings.get("accel")
        return {
            "ports": [{"id": p, "name": rc.port_name(p)} for p in sorted(self.brick.paths)],
            "left": left, "right": right, "invert_left": inv_l, "invert_right": inv_r,
            "trim": self.settings.get("trim", 0), "trim_range": rc.TRIM_RANGE,
            "accel": accel if accel in dict(rc.ACCELERATIONS) else rc.DEFAULT_ACCEL,
            "accels": [{"name": n, "seconds": s} for n, s in rc.ACCELERATIONS],
            "choice": rc.cal_predict(inv_l, inv_r),   # pre-filled Calibrate answers
            "test_ms": rc.TEST_MS,
        }

    def _safety(self):
        """Stop the robot if the phone that's driving goes quiet (Wi-Fi drop, app switch...)."""
        while not self.stop_event.is_set():
            time.sleep(0.05)
            with self.lock:
                if self.moving and time.monotonic() - self.last_command > PHONE_TIMEOUT:
                    self.stop()

    # ----- telemetry -----
    def _monitor(self):
        while not self.stop_event.is_set():
            try:
                if not self.brick.connected:
                    self.status = f"Connecting to {self.host}…"
                    self.brick.connect()
                    self.settings_mtime = None   # now its name is known: its own settings
                self.readings = self.brick.read_motors()
                self._check_ports()
                self.status = "Connected"
                self._update_telemetry()
            except rc.MotorsChanged:
                # Reconnect now, without a "disconnected" pause: the phone keeps its last
                # telemetry meanwhile, and the next heartbeat drives the newly picked pair.
                self.brick.drop()
                continue
            except Exception as e:
                self.brick.ctl = None
                self.brick.rtt = None
                self.status = f"Can't reach {self.host}: {e}"
                self._update_telemetry()
                time.sleep(2)
                continue
            time.sleep(rc.MONITOR_SECONDS)
        if self.brick.connected:   # shut down while connecting: let that connection go too
            self.brick.close()

    def shutdown(self):
        """No phone uses this brick any more: stop its motors and let it go."""
        self.stop_event.set()
        with self.lock:
            self.stop()
        self.brick.close()

    def _update_telemetry(self):
        self._reload_settings()
        lp, rp = self._ports()
        inv_l, inv_r = self._inverts()
        left, right = self.readings.get(lp), self.readings.get(rp)
        ls = rs = 0
        speed, gear = 0.0, "N"
        if left and right and lp != rp:
            ls, rs = left[0] * (-1 if inv_l else 1), right[0] * (-1 if inv_r else 1)
            forward = rc.deg_to_cm((ls + rs) / 2)
            spin = abs(ls - rs) > 40 and abs(ls + rs) < abs(ls - rs) / 2
            gear = ("↻" if ls > rs else "↺") if spin else "D" if forward > 0.5 else "R" if forward < -0.5 else "N"
            speed = rc.deg_to_cm((abs(ls) + abs(rs)) / 2) if spin else abs(forward)
            self.top_speed = max(self.top_speed, speed)
            pos = (left[1] * (-1 if inv_l else 1), right[1] * (-1 if inv_r else 1))
            if self.last_pos is not None:
                dl, dr = pos[0] - self.last_pos[0], pos[1] - self.last_pos[1]
                if abs(dl) < 5000 and abs(dr) < 5000:   # ignore jumps from position resets
                    self.trip_cm += abs(rc.deg_to_cm((dl + dr) / 2))
            self.last_pos = pos
        battery = None
        if self.brick.battery:
            volts, tech = self.brick.battery
            lo, hi = rc.BATTERY_RANGE.get(tech, rc.AA_RANGE)
            battery = max(0.0, min(1.0, (volts - lo) / (hi - lo)))
        known = sorted(self.brick.paths)
        warning = ("Plug in two motors" if len(known) < 2 else
                   "Pick two different motors in Setup" if lp == rp else
                   f"Motor {lp[-1]} / {rp[-1]} not found" if lp not in known or rp not in known else "")
        self.telemetry = {
            "connected": self.brick.connected and self.status == "Connected",
            "status": self.status,
            "brick_ms": round(self.brick.rtt * 1000) if self.brick.rtt else None,
            "battery": battery,
            "speed": round(speed, 1), "gear": gear,
            "left": ls, "right": rs, "max_deg": rc.MAX_SPEED,
            "trip_cm": round(self.trip_cm, 1), "top_speed": round(self.top_speed, 1),
            "mode": self.mode,
            "modes": [{"name": n, "pct": p, "cm": round(rc.deg_to_cm(rc.MAX_SPEED * p / 100))}
                      for n, p in rc.MODES],
            "max_cm": round(rc.deg_to_cm(rc.MOTOR_LIMIT * 1.15), 1),
            "motors": f"L = Motor {lp[-1]} · R = Motor {rp[-1]}",
            "warning": warning,
        }

    def _connection_state(self):
        connected = self.brick.connected and self.status == "Connected"
        return {"host": self.host, "name": self.brick.name if connected else None,
                "ip": self.brick.address if connected else None}

    def state(self, client=None):
        with self.lock:
            self._reload_settings()
            setup = self._setup_state()
            control = self.control(client)
        return {**self.telemetry, "mode": self.mode, "setup": setup, "connection": self._connection_state(),
                "control": control}


class Hub:
    """The bricks this server drives: one Controller per brick a phone has picked, and which
    brick each phone drives. Phones on different bricks drive them independently; phones on
    the same brick share it (one drives at a time). New phones start on this PC's brick."""

    def __init__(self):
        self.lock = threading.Lock()
        self.controllers = {}   # host -> Controller
        self.picked = {}        # phone id -> host
        self.seen = {}          # phone id -> when it last asked for anything (time.monotonic)
        self.default = rc.HOST  # where a phone starts: this PC's brick (ev3_config.json)
        self.search = ev3_find.BrickSearch((rc.USER, rc.PASSWORD))   # Find bricks, for every phone
        threading.Thread(target=self._reap_loop, daemon=True).start()

    def _get(self, host):
        """This brick's Controller, connecting to it if it's new (call with the lock held)."""
        if host not in self.controllers:
            self.controllers[host] = Controller(host)
        return self.controllers[host]

    def controller(self, client):
        """The Controller of the brick this phone picked (this PC's brick until it picks one)."""
        with self.lock:
            if client:
                self.seen[client] = time.monotonic()
            return self._get(self.picked.get(client, self.default))

    def select(self, client, address, remember=False):
        """This phone picks a brick (name or IP address). Other phones keep theirs. Returns an
        error, or ""."""
        host = rc.ev3_config.parse_address(address)
        if not host:
            return "Type the brick's name (like ev3kishan) or its IP address (like 192.168.1.23)."
        with self.lock:
            old = self.controllers.get(self.picked.get(client, self.default))
            if client:
                self.picked[client], self.seen[client] = host, time.monotonic()
            else:
                self.default = host   # a page without an id: it can only follow the default
            if old is not None and old.host != host and old.driver == client:
                with old.lock:
                    old.stop()   # stop what this phone was driving before it moves on
            self._get(host)
            self._release_unused()
        if remember:
            self.default = host   # new phones start on it; so does the desktop app and the next run
            try:
                rc.ev3_config.save_host(host)
            except OSError as e:
                return f"Connecting, but couldn't remember it: {e.strerror or e}"
        return ""

    def _release_unused(self, now=None):
        """Let go of bricks no phone has used for REAP_AFTER, and forget those phones (call
        with the lock held). This PC's brick is always kept: new phones start there."""
        now = time.monotonic() if now is None else now
        for client in [c for c, t in self.seen.items() if now - t > REAP_AFTER]:
            self.seen.pop(client, None)
            self.picked.pop(client, None)
        in_use = set(self.picked.values()) | {self.default}
        for host in [h for h in self.controllers if h not in in_use]:
            self.controllers.pop(host).shutdown()

    def _reap_loop(self):
        while True:
            time.sleep(5)
            with self.lock:
                self._release_unused()

    def state(self, client=None):
        state = self.controller(client).state(client)
        state["connection"].update(saved=rc.ev3_config.saved_host(), scan=self.search.state)
        return state

    def close(self):
        with self.lock:
            for controller in self.controllers.values():
                controller.shutdown()
            self.controllers.clear()


hub = None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, address reuse lets a second copy take a port that's already in use
    # (and phones then reach either one), so there a busy port must fail instead.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        # A phone closing the tab or switching apps mid-request is normal; don't print tracebacks.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:   # read each time so edits show up on refresh
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.partition("?")[0] == "/state":
            query = parse_qs(self.path.partition("?")[2])
            self._json(hub.state(client_id(query.get("c", [None])[0])))
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        client = client_id(data.get("client"))
        controller = hub.controller(client)   # the brick this phone picked
        if self.path == "/drive":
            controller.drive(data.get("held", []), data.get("mode"), data.get("stick"), client)
        elif self.path == "/stop":
            controller.request_stop(client, data.get("explicit") is True)
        elif self.path == "/takeover":
            controller.take_over(client)
        elif self.path == "/mode":
            with controller.lock:
                if data.get("mode") in dict(rc.MODES) and controller._claim(client, False):
                    controller.set_mode(data["mode"])
                    if controller.held or controller.stick:
                        controller._send()
        elif self.path == "/reset":
            controller.reset_trip()
        elif self.path == "/horn":
            with controller.lock:   # one writer at a time on the brick's command shell
                controller.brick.horn()
        elif self.path == "/setup":
            controller.update_setup(data)
        elif self.path == "/cal/test":
            controller.cal_test(data.get("arrow"), client)
        elif self.path == "/connect":
            error = hub.select(client, data.get("address"), data.get("remember") is True)
            return self._json({**hub.state(client), "error": error})
        elif self.path == "/scan":
            hub.search.start()
        elif self.path == "/cal/save":
            error = controller.save_calibration(data.get("choice"))
            return self._json({**hub.state(client), "saved": not error, "error": error})
        else:
            return self.send_error(404)
        self._json(hub.state(client))

    def log_message(self, *args):   # keep the console quiet (10 requests a second)
        pass


def client_id(value):
    """A phone page's random id as sent (letters, digits, dashes; at most 64), or None."""
    if isinstance(value, str) and 0 < len(value) <= 64 and all(c.isalnum() or c == "-" for c in value):
        return value
    return None


def start_server(wanted=None):
    """The web server on the `wanted` port, or on a random free port in PORT_RANGE.
    Trying to open the server is the check, so no other program can take the port
    between checking and using it."""
    if wanted:
        try:
            return Server(("0.0.0.0", wanted), Handler)
        except OSError as e:
            sys.exit(f"Can't use port {wanted} ({e.strerror or e}).\n"
                     f"Something else is using it: pick another, or leave out --port for a random free one.")
    ports = list(range(PORT_RANGE[0], PORT_RANGE[1] + 1))
    random.shuffle(ports)
    for port in ports:
        try:
            return Server(("0.0.0.0", port), Handler)
        except OSError:
            continue   # in use: try another
    sys.exit(f"No free port between {PORT_RANGE[0]} and {PORT_RANGE[1]}.")


def main():
    global hub
    server = start_server(PORT_ARG)
    port = server.server_address[1]
    hub = Hub()
    print("\nEV3 RC phone controller")
    print(f"  this PC's brick: {rc.HOST} (each phone can pick its own)")
    for ip in lan_addresses():
        print(f"  open on your phone:  http://{ip}:{port}")
    print("  (phone must be on the same Wi-Fi; allow Python through the firewall if asked)")
    print("  Ctrl+C to quit")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("stopping…")
        hub.close()


if __name__ == "__main__":
    main()
