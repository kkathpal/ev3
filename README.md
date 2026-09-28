# EV3 Dashboard

Drive and monitor a LEGO Mindstorms EV3 running [ev3dev](https://www.ev3dev.org/) from your
computer or phone, over the same SSH connection the VS Code EV3 extension uses.

- **EV3 RC** (`ev3_drive.pyw`): drive a two-motor robot with the arrow keys / WASD, with a live
  speedometer, trip meter and battery.
- **Phone controller** (`ev3_phone.py`): the same driving from any phone on your Wi-Fi.
- **EV3 Status** (`ev3_widget.pyw`): a small always-on-top window with the brick's battery, CPU,
  memory, ports, motors and sensors.

## Setup

1. Install Python 3 (python.org).
   - macOS: use the python.org installer (it includes a current Tk). Apple's built-in
     `/usr/bin/python3` has an old Tk that can show blank windows. With Homebrew Python, also
     run `brew install python-tk`.
2. `pip install -r requirements.txt`
3. Turn on the brick and connect it (USB, Bluetooth or Wi-Fi) as you would for VS Code.
   On a Mac, Wi-Fi (a USB Wi-Fi dongle on the brick) or USB is easiest; recent macOS versions
   no longer support Bluetooth networking.

## Run

| | Windows | macOS / Linux |
|---|---|---|
| Drive | `python ev3_drive.pyw` (or double-click) | `python3 ev3_drive.pyw` |
| Phone | `python ev3_phone.py` | `python3 ev3_phone.py` |
| Status | `python ev3_widget.pyw` (or double-click) | `python3 ev3_widget.pyw` |

The brick's address and login live in `ev3_config.json`, created next to the apps on first run:

```json
{
  "host": "ev3dev.local",
  "user": "robot",
  "password": "maker"
}
```

If you rename the brick (`sudo hostnamectl set-hostname mycar` on the brick, also replace `ev3dev`
in its `/etc/hosts`, then reboot), set `"host": "mycar.local"`. If `.local` names don't connect,
use the brick's IP address. For a one-off, an address on the command line wins:
`python ev3_drive.pyw 192.168.0.1`. The file is not committed to git, so each computer has its own. For the phone, open the `http://…:8080` address it prints
(the phone must be on the same Wi-Fi; allow Python through the firewall if asked).

## Driving

| Key | Action |
|---|---|
| ↑ ↓ / W S | forward / backward |
| ← → / A D | turn (with ↑/↓ to curve, alone to spin in place) |
| 1 2 3 4, + − | gear: Slow / Normal / Fast / Turbo |
| Space | stop hard |

First time with a robot: in the **Setup** card pick which motor is each wheel, press **Test** to
check it rolls forward (tick **Invert** if not), and use **Drift fix** if it curves when it should
go straight. Settings are saved in `ev3_drive_settings.json` next to the app (one per computer).

To go easy on the gears the robot speeds up gradually. Tune `RAMP_SECONDS`, `STEER_SECONDS`,
`RELEASE_STOP` and `HARD_STOP` at the top of `ev3_drive.pyw`.

## Tests

`python -m unittest discover -s tests -v` checks the driving logic without a robot.
