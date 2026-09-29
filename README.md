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

## One brick per laptop

Each laptop can have its own copy of this project and drive its own brick. On every laptop:

1. Get the code: on [github.com/kkathpal/ev3](https://github.com/kkathpal/ev3) click
   **Code → Download ZIP** and unzip it, or run `git clone https://github.com/kkathpal/ev3.git`.
2. Run the installer in the project folder:
   - **Windows:** double-click `install.bat`. It installs Python if it's missing (using
     winget), then the packages, asks for this laptop's brick (e.g. `ev3kishan`), and can add
     an **EV3 RC** shortcut to the desktop.
   - **Mac / Linux:** run `sh install.sh`. It installs the packages and asks for the brick. It
     needs Python 3 from python.org first (see [Setup](#setup)).
3. Connect that brick to that laptop (Bluetooth, USB or Wi-Fi) and start **EV3 RC**.

The brick setting is saved in `ev3_config.json`, which stays on that laptop (it's not uploaded
to GitHub), so each laptop keeps its own brick. To change it later, run the installer again or
`python ev3_config.py <brick name or IP>`. Run `python ev3_config.py` with no name to see which
brick the laptop is set to.

If the name isn't found, use the IP address shown on the brick's screen instead, for example
`python ev3_config.py 192.168.0.1`. To get the newest code later, run `git pull` (or download
the ZIP again, keeping your `ev3_config.json`). Your brick setting isn't changed. The phone
controller works on every laptop at the same time, since each picks its own free port.

## Run

| | Windows | macOS / Linux |
|---|---|---|
| Drive | `python ev3_drive.pyw` (or double-click) | `python3 ev3_drive.pyw` |
| Phone | `python ev3_phone.py` | `python3 ev3_phone.py` |
| Status | `python ev3_widget.pyw` (or double-click) | `python3 ev3_widget.pyw` |

For the phone, the server picks a random free port (8000–8999) each time, or the one you give with
`python ev3_phone.py --port 8090`. Open the `http://…:<port>` address it prints (the phone must be
on the same Wi-Fi; allow Python through the firewall if asked).

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
in its `/etc/hosts`, then reboot), run `python ev3_config.py mycar` (or set
`"host": "mycar.local"` in the file). If `.local` names don't connect, use the brick's IP address. For a one-off, an address on the command line wins:
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

First time with a robot: in the **Setup** card press **Calibrate…**. Hold each arrow key (or its
**Test** button); the robot moves slowly while you hold it. Click what it actually did (Forward,
Backward, Left or Right) and **Save**: it works out **Swap** and **Invert** for you. You can also
do it by hand: pick which motor is each wheel, press **Test** to check it rolls forward (tick
**Invert** if not). Use **Drift fix** if it curves when it should go straight. Settings are saved
in `ev3_drive_settings.json` next to the app (one per computer), and the phone uses them too.

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

## Setting up more bricks

To make several bricks the same (each keeps its own name), `ev3_setup.py` copies your EV3
programs to a brick. These are the folders next to this one that have a `main.py`, made with
the VS Code EV3 extension, like `../brick1_test`. They go to `/home/robot/<folder>` on the
brick, just like the extension's "Download and Run". The script also checks the brick's name,
battery, motors and free space. It never moves a motor or deletes anything on the brick.

```
python ev3_setup.py 192.168.0.1 --dry-run   # list what would be copied
python ev3_setup.py 192.168.0.1             # copy (use each brick's address in turn)
```

Run it once per brick, and again after you change a program. Then drive any brick by giving
its address: `python ev3_drive.pyw ev3kishan.local` (or its IP).

Over Bluetooth, the PC needs a network connection to the brick, not just pairing. On the brick,
turn on Wireless and Networks → Tethering → Bluetooth. On Windows, in Devices and Printers,
right-click the brick → Connect using → Access point. Windows usually keeps only one such
connection at a time, so set up one brick after another. A USB Wi-Fi dongle on each brick lets
them all be reachable at once.

## Troubleshooting

- **Nothing happens when I double-click** – run it from a terminal instead
  (`python ev3_drive.pyw`) to see the error. A missing `paramiko` shows a message box.
- **"Connecting…" forever / "getaddrinfo failed"** – the computer can't find the brick. Check it
  is connected (the brick's screen shows an IP address), then use that IP:
  `python ev3_config.py <IP>`.
- **"Authentication failed"** – the brick's password isn't `maker`; fix it in `ev3_config.json`.
- **The robot drives the wrong way or curves** – use the Setup card (Calibrate…, or Test, Invert,
  Swap, Drift fix).
- **Phone: "Can't use port …"** – something else (maybe another copy) is using the port you gave
  with `--port`; close it, pick another, or leave out `--port` to get a random free one.

## Tests

`python -m unittest discover -s tests -v` checks the driving logic and the brick setup script
without a robot.
