"""Brick connection settings shared by the EV3 apps, kept in ev3_config.json next to them.

The file is created with ev3dev's defaults on first run. It is not in git, so every laptop
keeps its own: set which brick this laptop drives with

    python ev3_config.py ev3kishan        (a brick name; ".local" is added)
    python ev3_config.py 192.168.0.1      (or its IP address)

or edit the file (also for another login). An address on the command line of an app
still wins, for a one-off:   python ev3_drive.pyw 192.168.0.1
"""
import json
import os
import socket
import sys

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ev3_config.json")
DEFAULTS = {"host": "ev3dev.local", "user": "robot", "password": "maker"}


def load():
    """Return {"host", "user", "password"}: the file's values over the defaults, and the
    command-line address over both."""
    config = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        config.update({k: v for k, v in saved.items() if k in DEFAULTS and isinstance(v, str) and v})
    except FileNotFoundError:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS, f, indent=2)
                f.write("\n")
        except OSError:
            pass
    except (OSError, ValueError, AttributeError):
        pass   # unreadable or not a JSON object: fall back to the defaults
    if len(sys.argv) > 1:
        config["host"] = sys.argv[1]
    return config


def saved_host():
    """This laptop's brick as saved in the file (a command-line address doesn't count)."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            host = json.load(f).get("host")
    except (OSError, ValueError, AttributeError):
        host = None
    return host if isinstance(host, str) and host else DEFAULTS["host"]


def ssh_address(host, port=22):
    """Where to open SSH to `host`: its first IPv4 address when it has one. Windows can list
    a .local name's IPv6 link-local address first, which ev3dev's SSH doesn't answer on, and
    paramiko gives up after that one timed-out attempt instead of trying the next address."""
    try:
        return socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (OSError, IndexError):
        return host   # no IPv4 address (or not found right now): let the connect report it


def brick_address(name):
    """A brick name as typed ("ev3kishan") to an address: bare names get ".local" (how
    ev3dev bricks are found on the network); IPs and full names stay as they are."""
    name = name.strip()
    return name if "." in name or ":" in name else f"{name}.local"


def save_host(host):
    """Make `host` this laptop's brick, keeping the rest of the file."""
    config = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            config.update({k: v for k, v in saved.items() if k in DEFAULTS and isinstance(v, str) and v})
    except (OSError, ValueError):
        pass
    config["host"] = host
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        current = load()["host"] if len(sys.argv) == 1 else "?"
        sys.exit(f"This laptop drives: {current}\n"
                 f"To change it:  python ev3_config.py <brick name or IP>   (e.g. ev3kishan)")
    host = brick_address(sys.argv[1])
    save_host(host)
    print(f"This laptop now drives {host}.\n"
          f"(Saved in ev3_config.json, which stays on this laptop.) Start it with:  python ev3_drive.pyw")
