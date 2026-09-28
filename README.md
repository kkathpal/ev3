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
2. In this folder: `python -m pip install -r requirements.txt` (on macOS/Linux: `python3 -m pip …`)
3. Turn on the brick and connect it (USB, Bluetooth or Wi-Fi) as you would for VS Code.
   On a Mac, Wi-Fi (a USB Wi-Fi dongle on the brick) or USB is easiest; recent macOS versions
   no longer support Bluetooth networking.

## Run

| | Windows | macOS / Linux |
|---|---|---|
| Drive | `python ev3_drive.pyw` (or double-click) | `python3 ev3_drive.pyw` |
| Phone | `python ev3_phone.py` | `python3 ev3_phone.py` |
| Status | `python ev3_widget.pyw` (or double-click) | `python3 ev3_widget.pyw` |

For the phone, open the `http://…:8080` address it prints (the phone must be on the same Wi-Fi;
allow Python through the firewall if asked).

On smaller screens, click a card's title (▾) to fold it away; the apps fold the least-needed
cards themselves when the window wouldn't fit.

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
`python ev3_drive.pyw 192.168.0.1`. The file is not committed to git, so each computer has its own.

## Driving

| Key | Action |
|---|---|
| ↑ ↓ / W S | forward / backward |
| ← → / A D | turn (with ↑/↓ to curve, alone to spin in place) |
| 1 2 3 4, + − | gear: Slow / Normal / Fast / Turbo |
| H | horn (also a HORN button on the phone) |
| P | play the sound picked in the Sound card |
| Space | stop hard |

First time with a robot: in the **Setup** card pick which motor is each wheel, press **Test** to
check it rolls forward (tick **Invert** if not), and use **Drift fix** if it curves when it should
go straight. Settings are saved in `ev3_drive_settings.json` next to the app (one per computer).

To go easy on the gears the robot speeds up gradually. Tune `RAMP_SECONDS`, `STEER_SECONDS`,
`RELEASE_STOP` and `HARD_STOP` at the top of `ev3_drive.pyw`.

## Sound

EV3 RC and the status widget both have a **Sound** card that plays sounds on the brick's
speaker (EV3 RC also has a **HORN** button):

- **Pick a sound**: the brick's ~100 built-in effects (animals, horns, voices, numbers…) and your
  own uploads under *my sounds*. Picking one plays it; **▶ Play** replays, **■** stops.
- **Say**: type text and press Enter or Say; the robot speaks it, and it's kept under
  *my sounds* (as `said_….wav` in `~/sounds`) so you can replay it with ▶ Play or **P**.
- **Upload…**: copies a `.wav` file from your computer to `~/sounds` on the brick. MP3 isn't
  supported on the brick, so convert to WAV first (the speaker is small and mono, so short,
  loud clips work best).
- **Volume** − / +: the brick's speaker volume.

## Troubleshooting

- **Nothing happens when I double-click** – run it from a terminal instead
  (`python ev3_drive.pyw`) to see the error. A missing `paramiko` shows a message box.
- **"Connecting…" forever / "getaddrinfo failed"** – the computer can't find the brick. Check it
  is connected (the brick's screen shows an IP address), then put that IP in `ev3_config.json`.
- **"Authentication failed"** – the brick's password isn't `maker`; fix it in `ev3_config.json`.
- **The robot drives the wrong way or curves** – use the Setup card (Test, Invert, Swap, Drift fix).
- **Phone: "Can't use port 8080"** – another copy is already running; close it first.

## Tests

`python -m unittest discover -s tests -v` checks the driving logic without a robot.
