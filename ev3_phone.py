"""EV3 RC for phones — drive the robot from any phone browser on your Wi-Fi.

This PC keeps the connection to the brick (like ev3_drive.pyw) and serves a touch
controller page to phones on the same Wi-Fi. It drives exactly like the desktop
app: same gears, Turbo, and calibration (read from ev3_drive_settings.json).

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

PORT_RANGE = (8000, 8999)   # each run uses a random free port from here (--port N picks one)
PAGE = os.path.join(HERE, "ev3_phone.html")
PHONE_TIMEOUT = 0.35   # stop if the driving phone sends nothing for this long (s)
DIRECTIONS = {"up", "down", "left", "right"}


def parse_stick(value):
    """A /drive request's joystick position as (x, y), each clamped to -1..1, or None if it
    isn't one (json.loads accepts NaN and Infinity, which would reach the motors)."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
        return None
    return tuple(max(-1.0, min(1.0, float(v))) for v in value)


class Controller:
    """Owns the brick connection; turns the phone's joystick (or held keys) into drive pulses."""

    def __init__(self):
        self.brick = rc.Brick()
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
        threading.Thread(target=self._monitor, daemon=True).start()
        threading.Thread(target=self._safety, daemon=True).start()

    # ----- settings shared with the desktop app -----
    def _reload_settings(self):
        """Pick up calibration changes made in ev3_drive.pyw while this runs."""
        try:
            mtime = os.path.getmtime(rc.SETTINGS_FILE)
        except OSError:
            mtime = None
        if mtime != self.settings_mtime:
            self.settings, self.settings_mtime = rc.load_settings(), mtime

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
    def drive(self, held, mode=None, stick=None):
        with self.lock:
            if mode in dict(rc.MODES) and mode != self.mode:
                self.set_mode(mode)
            self.stick = parse_stick(stick)
            self.held = {d for d in held if d in DIRECTIONS} if isinstance(held, list) else set()
            self.last_command = time.monotonic()
            self._send()

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
        rc.save_settings({**self.settings, "mode": mode})   # desktop app opens in the same gear
        self.settings_mtime = None

    def reset_trip(self):
        self.trip_cm = self.top_speed = 0.0

    # ----- Setup · Calibrate from the phone (same as the desktop's Setup card) -----
    def cal_test(self, arrow):
        """Calibrate by driving: run the picked motors in CAL_PATTERNS[arrow] (no Invert, slow
        TEST_SPEED timed pulses) while the phone keeps re-sending it; _safety stops it after."""
        with self.lock:
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
        rc.save_settings({**self.settings, **changes})
        self.settings_mtime = None   # re-read now

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
        while True:
            time.sleep(0.05)
            with self.lock:
                if self.moving and time.monotonic() - self.last_command > PHONE_TIMEOUT:
                    self.stop()

    # ----- telemetry -----
    def _monitor(self):
        while True:
            try:
                if not self.brick.connected:
                    self.status = "Connecting…"
                    self.brick.connect()
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
                self.status = f"Brick disconnected: {e}"
                self._update_telemetry()
                time.sleep(2)
                continue
            time.sleep(rc.MONITOR_SECONDS)

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

    def state(self):
        with self.lock:
            self._reload_settings()
            setup = self._setup_state()
        return {**self.telemetry, "mode": self.mode, "setup": setup}


controller = None


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
        elif self.path == "/state":
            self._json(controller.state())
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/drive":
            controller.drive(data.get("held", []), data.get("mode"), data.get("stick"))
        elif self.path == "/stop":
            with controller.lock:
                controller.stop()
        elif self.path == "/mode":
            with controller.lock:
                if data.get("mode") in dict(rc.MODES):
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
            controller.cal_test(data.get("arrow"))
        elif self.path == "/cal/save":
            error = controller.save_calibration(data.get("choice"))
            return self._json({**controller.state(), "saved": not error, "error": error})
        else:
            return self.send_error(404)
        self._json(controller.state())

    def log_message(self, *args):   # keep the console quiet (10 requests a second)
        pass


def lan_addresses():
    """This PC's addresses a phone on the same network could reach."""
    found = []
    try:   # the interface the default route uses (no packets are sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:   # can fail on macOS when the computer's own name doesn't resolve
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip not in found and not ip.startswith("127.") and ip != "192.168.0.2":   # skip the brick link
            found.append(ip)
    return found


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
    global controller
    server = start_server(PORT_ARG)
    port = server.server_address[1]
    controller = Controller()
    print("\nEV3 RC phone controller")
    print(f"  brick: {rc.HOST}")
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
        with controller.lock:
            controller.stop()
        controller.brick.close()


if __name__ == "__main__":
    main()
