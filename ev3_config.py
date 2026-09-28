"""Brick connection settings shared by the EV3 apps, kept in ev3_config.json next to them.

The file is created with ev3dev's defaults on first run. Edit it to use another brick name
(e.g. after renaming the brick with hostnamectl) or login. An address on the command line
still wins, for a one-off:   python ev3_drive.pyw 192.168.0.1
"""
import json
import os
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
