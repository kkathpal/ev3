"""Finding EV3 bricks and switching between them, shared by EV3 RC (ev3_drive.pyw) and the phone
controller (ev3_phone.py).

find_bricks() looks on this PC's networks: first Bluetooth/USB links to a brick, then the Wi-Fi.
Each device that answers SSH (OpenSSH, as on ev3dev) is logged in to with the brick login from
ev3_config.json (robot / maker by default) and kept only if it's an EV3 brick; the brick then tells
its own name, battery and motors. paired_bluetooth_bricks() lists bricks paired over Bluetooth whose
Bluetooth network isn't connected yet (Windows). BrickSearch runs both in the background for a UI.
"""
import ipaddress
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

SCAN_PREFIX = 22       # search this size of network around the PC's Wi-Fi address
LINK_PREFIX = 24       # ...and around a Bluetooth/USB link to a brick (a small network)
SCAN_TIMEOUT = 0.4     # ...waiting this long (s) for each address to answer
PAIRED_TIMEOUT = 20    # give up on the list of paired Bluetooth devices after this (s)
LOGIN_TIMEOUT = 6      # give up logging in to a device after this (s)

# Run on each device logged in to: an EV3 has the EV3 battery; also its name and motors
IDENTIFY = r"""
b=/sys/class/power_supply/lego-ev3-battery
[ -d $b ] || { echo not-ev3; exit; }
read v < $b/voltage_now; echo "battery $v"
echo "name $(hostname)"
for m in /sys/class/tacho-motor/motor*; do [ -d "$m" ] && read a < $m/address && echo "motor ${a##*:}"; done
"""


def lan_addresses(brick_link=False):
    """This PC's addresses a phone on the same network could reach (with brick_link, also its
    end of a Bluetooth link to a brick, 192.168.0.2, which phones can't reach)."""
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
        if ip not in found and not ip.startswith("127.") and (brick_link or ip != "192.168.0.2"):
            found.append(ip)
    return found


def default_route_ip():
    """This PC's address on its main network (normally Wi-Fi), or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))   # picks the route; no packets are sent
            return s.getsockname()[0]
    except OSError:
        return None


def scan_networks():
    """The networks to search for bricks, as [(network, "Wi-Fi" or "Bluetooth / USB")]: around
    each of this PC's private IPv4 addresses, SCAN_PREFIX bits wide (not link-local or public).
    The main network is the Wi-Fi; the others are links to a brick. Those go first."""
    main = default_route_ip()
    nets = []
    for ip in lan_addresses(brick_link=True):
        via = "Wi-Fi" if ip == main else "Bluetooth / USB"
        try:
            net = ipaddress.ip_network(f"{ip}/{SCAN_PREFIX if via == 'Wi-Fi' else LINK_PREFIX}", strict=False)
        except ValueError:
            continue
        if net.is_private and not net.is_link_local and not net.is_loopback and net not in [n for n, _ in nets]:
            nets.append((net, via))
    return sorted(nets, key=lambda item: item[1] == "Wi-Fi")   # links first: small and quick


def ssh_banner(ip):
    """The SSH greeting at `ip`, or None. Reading it doesn't log in."""
    try:
        with socket.create_connection((ip, 22), timeout=SCAN_TIMEOUT) as s:
            s.settimeout(1.5)
            return s.recv(80).decode(errors="replace").strip()
    except OSError:
        return None


def identify(ip, user, password):
    """Log in to `ip` with the brick login. Returns {"name", "battery" (volts), "motors"} if it's
    an EV3 brick, or None (not an EV3, wrong login, no answer)."""
    import paramiko   # only needed here; the rest of this module is the standard library
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=user, password=password, timeout=LOGIN_TIMEOUT,
                       banner_timeout=LOGIN_TIMEOUT, auth_timeout=LOGIN_TIMEOUT,
                       look_for_keys=False, allow_agent=False)
        _, out, _ = client.exec_command(IDENTIFY, timeout=LOGIN_TIMEOUT)
        text = out.read().decode(errors="replace")
    except Exception:
        return None
    finally:
        client.close()
    brick = {"name": None, "battery": None, "motors": []}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "not-ev3":
            return None
        if key == "battery" and value.strip().isdigit():
            brick["battery"] = round(int(value) / 1e6, 1)
        elif key == "name":
            brick["name"] = value.strip() or None
        elif key == "motor":
            brick["motors"].append(value.strip())
    return brick if brick["battery"] is not None else None


def find_bricks(report=None, user="robot", password="maker"):
    """EV3 bricks on this PC's networks, as [{"ip", "via", "name", "battery", "motors"}]: every
    device answering SSH (OpenSSH) is logged in to with `user`/`password` and kept if it's an EV3.
    `report(found_so_far)` is called as bricks are confirmed."""
    own = set(lan_addresses(brick_link=True))
    nets = scan_networks()
    found, probed = [], set()

    def probe(hosts, via):
        hosts = [h for h in hosts if h not in own and h not in probed]
        probed.update(hosts)
        banners = list(pool.map(ssh_banner, hosts))
        candidates = [ip for ip, banner in zip(hosts, banners) if banner and "OpenSSH" in banner]
        for ip, brick in zip(candidates, pool.map(lambda ip: identify(ip, user, password), candidates)):
            if brick:
                found.append({"ip": ip, "via": via, **brick})
                if report:
                    report(found)

    with ThreadPoolExecutor(128) as pool:
        # A brick on a Bluetooth/USB link is nearly always its .1 address: check those first
        for net, via in nets:
            if via != "Wi-Fi":
                probe([str(net.network_address + 1)], via)
        for net, via in nets:
            probe([str(h) for h in net.hosts()], via)
    return found


def paired_bluetooth_bricks():
    """Names of EV3 bricks paired with this PC over Bluetooth, e.g. ["ev3kk"] (Windows only).
    Being paired isn't enough to drive: the Bluetooth network to the brick must be connected."""
    if sys.platform != "win32":
        return []
    command = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object { "
               "$_.FriendlyName -like 'ev3*' -and $_.FriendlyName -notmatch 'Avrcp|Transport' } | "
               "ForEach-Object { $_.FriendlyName }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True,
                             text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


class BrickSearch:
    """One search at a time, in the background. `state` is what a UI shows:
    {"running", "found": [{"ip", "via", "name", "battery", "motors"}], "paired": [names], "error"};
    bricks appear in "found" as soon as they're confirmed. `login` is (user, password)."""

    def __init__(self, login=("robot", "maker")):
        self.login = login
        self.lock = threading.Lock()
        self.state = {"running": False, "found": [], "paired": [], "error": ""}

    def start(self):
        """Start a search unless one is running. Returns True if it started."""
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "found": [], "paired": [], "error": ""}
        threading.Thread(target=self.run, daemon=True).start()
        return True

    def run(self):
        paired = []   # filled in by its own thread while the networks are searched
        lookup = threading.Thread(target=lambda: paired.extend(paired_bluetooth_bricks()), daemon=True)
        lookup.start()
        try:
            found = find_bricks(report=lambda so_far: self.state.update(found=list(so_far)),
                                user=self.login[0], password=self.login[1])
            error = ""
        except Exception as e:
            found, error = [], f"Search failed: {e}"
        lookup.join(PAIRED_TIMEOUT)
        names = {f.get("name") for f in found}
        waiting = [name for name in paired if name not in names]   # paired, but no network link yet
        if not found and not waiting and not error:
            error = ("No bricks found. A brick has to be on (with its IP address on its screen), and on "
                     "the same Wi-Fi as this PC or linked to it over Bluetooth/USB.")
        self.state = {"running": False, "found": found, "paired": waiting, "error": error}
