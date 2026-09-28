"""EV3 RC for phones — drive the robot from any phone browser on your Wi-Fi.

This PC keeps the connection to the brick (like ev3_drive.pyw) and serves a touch
controller page to phones on the same Wi-Fi. It drives exactly like the desktop
app: same gears, Turbo, and calibration (read from ev3_drive_settings.json).

Run:   python ev3_phone.py               (brick at ev3dev.local)
       python ev3_phone.py 192.168.0.1   (brick at a specific address)
Then open the printed http://<this PC>:8080 address on your phone.

Safety: the phone re-sends the held buttons every ~100 ms. Each command only runs
the motors briefly (Turbo is covered by the brick-side watchdog), and this server
also stops the robot if a phone goes quiet for PHONE_TIMEOUT, so lifting your
finger, locking the phone or losing Wi-Fi stops the robot.
"""
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
rc = SourceFileLoader("ev3_drive", os.path.join(HERE, "ev3_drive.pyw")).load_module()

PORT = 8080
PAGE = os.path.join(HERE, "ev3_phone.html")
PHONE_TIMEOUT = 0.35   # stop if the driving phone sends nothing for this long (s)
DIRECTIONS = {"up", "down", "left", "right"}


class Controller:
    """Owns the brick connection; turns phone button states into drive pulses."""

    def __init__(self):
        self.brick = rc.Brick()
        self.lock = threading.Lock()
        self.settings, self.settings_mtime = {}, None
        self._reload_settings()
        self.mode = self.settings.get("mode", "Normal")
        if self.mode not in dict(rc.MODES):
            self.mode = "Normal"
        self.held = set()
        self.moving = False
        self.last_command = 0.0
        self.status = "Connecting…"
        self.readings = {}
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

    def _inverts(self):
        legacy = self.settings.get("reverse", False)
        return (self.settings.get("invert_left", legacy), self.settings.get("invert_right", legacy))

    # ----- driving -----
    def drive(self, held, mode=None):
        with self.lock:
            if mode in dict(rc.MODES) and mode != self.mode:
                self.set_mode(mode)
            self.held = {d for d in held if d in DIRECTIONS}
            self.last_command = time.monotonic()
            self._send()

    def _send(self):
        self._reload_settings()
        inv_l, inv_r = self._inverts()
        left, right = rc.wheel_commands(self.held, self.mode, self.settings.get("trim", 0))
        lp_name, rp_name = self._ports()
        lp, rp = self.brick.paths.get(lp_name), self.brick.paths.get(rp_name)
        if (left or right) and self.brick.connected and lp and rp and lp != rp:
            rc.send_drive(self.brick, lp, rp, left, right, self.mode, inv_l, inv_r)
            self.moving = True
        elif self.moving or not self.held:
            self.stop(released=True)

    def stop(self, released=False):
        self.held = set()
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
                self.status = "Connected"
                self._update_telemetry()
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
                   "Pick two different motors in the desktop app" if lp == rp else
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
        return {**self.telemetry, "mode": self.mode}


controller = None


class Server(ThreadingHTTPServer):
    daemon_threads = True

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
            controller.drive(data.get("held", []), data.get("mode"))
        elif self.path == "/stop":
            with controller.lock:
                controller.stop()
        elif self.path == "/mode":
            with controller.lock:
                if data.get("mode") in dict(rc.MODES):
                    controller.set_mode(data["mode"])
                    if controller.held:
                        controller._send()
        elif self.path == "/reset":
            controller.reset_trip()
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
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if ip not in found and not ip.startswith("127.") and ip != "192.168.0.2":   # skip the brick link
            found.append(ip)
    return found


def main():
    global controller
    controller = Controller()
    server = Server(("0.0.0.0", PORT), Handler)
    print("EV3 RC phone controller")
    print(f"  brick: {rc.HOST}")
    for ip in lan_addresses():
        print(f"  open on your phone:  http://{ip}:{PORT}")
    print("  (phone must be on the same Wi-Fi; allow Python through the Windows firewall if asked)")
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
