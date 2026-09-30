# EV3 Dashboard

Drive and monitor a LEGO Mindstorms EV3 running [ev3dev](https://www.ev3dev.org/) from your
computer or phone, over the same SSH connection the VS Code EV3 extension uses.

- **EV3 RC** (`ev3_drive.pyw`): drive a two-motor robot with the arrow keys / WASD, with a live
  speedometer, trip meter and battery.
- **Phone controller** (`ev3_phone.py`): the same driving from any phone on your Wi-Fi, with a
  touch joystick.
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

**Several phones:** more than one phone can open the page, but only one drives at a time. The
first to move the robot holds the controls; the others show "🔒 Another phone is driving" and
can drive about a second after it lets go, or tap **Take control** (the robot stops first).
**STOP** works from every phone. A phone that's locked or switches app only stops the robot if
it's the one driving.

**Picking the brick, from the phone:** every time the phone page opens, it starts on the
**Connect to the brick** screen and searches for bricks: first ones connected to the PC over
**Bluetooth or USB**, then ones on the same **Wi-Fi**. Each is labelled with how it's
connected; tap one to connect, or tap **Done** to keep the current brick. You can also type a
brick's name (like `ev3kishan`) or its IP address (the brick's screen shows it at the top).
Bricks that are only *paired* over Bluetooth are listed with the steps to connect their
Bluetooth network. For Wi-Fi the brick needs a USB Wi-Fi dongle and must be on the same network
as the PC (on the brick: Wireless and Networks → Wi-Fi). With **Remember on this PC** ticked,
the desktop app uses that brick too. Open the screen again any time with **📶 Brick · Wi-Fi**
(below the gears) or **Connect another way…** on the "Waiting for the robot" screen. Each device that answers SSH is logged in to with the brick login in `ev3_config.json`
(robot / maker by default); only confirmed EV3 bricks are listed, with the name, battery and
motors the brick reports, and the one connected now is marked **Connected**.

The phone can also set the robot up: tap **⚙ Setup · Calibrate** (below the gears). It has
the same settings as the desktop's Setup card: which motor is each wheel, Invert, Swap,
**Calibrate by driving** (tap Test next to each arrow, tap what the robot did, then Save),
Drift fix and Acceleration. The phone and the desktop app share these settings.

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

## Picking the brick

Every time **EV3 RC** starts, a **Connect to the brick** window opens (as the phone page does).
It searches for bricks, first ones linked to this PC over **Bluetooth or USB**, then ones on the
same **Wi-Fi**, and labels each; click one to connect, or click **Done** to keep the current
brick. You can also type a brick's name (like `ev3kishan`) or its IP address (shown at the top
of the brick's screen). Bricks that are only *paired* over Bluetooth are listed with the steps
to connect their Bluetooth network. With **Remember on this PC** ticked, the choice is saved in
`ev3_config.json`, which the phone controller uses too. Reopen it any time with **📶 Brick…** in
the header. Each device that answers SSH is logged in to with the brick login in `ev3_config.json`
(robot / maker by default); only confirmed EV3 bricks are listed, with the name, battery and
motors the brick reports, and the one connected now is marked **Connected**.

## Driving

| Key | Action |
|---|---|
| ↑ ↓ / W S | forward / backward |
| ← → / A D | turn (with ↑/↓ to curve, alone to spin in place) |
| 1 2 3 4, + − | gear: Slow / Normal / Fast / Turbo |
| H | horn (also a HORN button on the phone) |
| P | play the sound picked in the Sound card |
| Space | stop hard |

On the phone, drive with two sticks like a game controller: the **left stick** goes forward and
back, the **right stick** goes left and right (each only moves along its own line). Use both thumbs
together: how far you push sets the speed, up to the selected gear's top speed at the rim; the right
stick alone spins in place, both together curve like ↑ + →, and every mix in between curves
smoothly. Let go to stop. **STOP** and **HORN** sit under them, and the gears still set the top
speed. Hold the phone sideways for a handheld layout: a stick under each thumb at the left and right
edges, and the speedometer, gears, STOP and HORN between them. If you open the phone page in a desktop browser, the arrow keys work there too.

First time with a robot: in the **Setup** card press **Calibrate…**. Hold each arrow key (or its
**Test** button); the robot moves slowly while you hold it. Click what it actually did (Forward,
Backward, Left or Right) and **Save**: it works out **Swap** and **Invert** for you. You can also
do it by hand: pick which motor is each wheel, press **Test** to check it rolls forward (tick
**Invert** if not). Use **Drift fix** if it curves when it should go straight. Settings are saved
in `ev3_drive_settings.json` next to the app (one per computer), and the phone uses them too.

To go easy on the gears the robot speeds up gradually. Pick how fast in the **Setup** card:
**Acceleration** Quick (0.6 s from stopped to full power, the default), Normal (1.2 s) or Gentle
(2.5 s, easiest on the gears). The phone uses the same setting. Tune `ACCELERATIONS`,
`STEER_SECONDS`, `RELEASE_STOP` and `HARD_STOP` at the top of `ev3_drive.pyw`.

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

## EV3 programs

The `programs` folder holds EV3 MicroPython programs that run on the brick itself, one folder
each, like `programs/brick1_test`. Open a program's folder in VS Code with the LEGO EV3
MicroPython extension and use **Download and Run**, or copy them all with `ev3_setup.py` (below).
Add a new program by creating it with the extension inside `programs`.

## Setting up more bricks

To make several bricks the same (each keeps its own name), `ev3_setup.py` copies your EV3
programs to a brick. These are the folders in `programs` that have a `main.py`. They go to
`/home/robot/<folder>` on the brick, just like the extension's "Download and Run". The script also checks the brick's name,
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
- **The robot doesn't move but the horn works** (say, after moving the motor cables to other
  sockets) – EV3 RC and the phone controller switch to the motors that are plugged in by
  themselves. Check which at the top of the phone page (`L = Motor B · R = Motor C`) or in
  Setup, and pick them there if they're wrong.
- **Phone: a code update doesn't show** – restart the server (Ctrl+C, then `python ev3_phone.py`
  again): reloading the page only picks up changes to the page itself, not to the Python files.
  Run just one copy of `ev3_phone.py` per laptop, since each copy connects to the brick.
- **Phone: "Can't use port …"** – something else (maybe another copy) is using the port you gave
  with `--port`; close it, pick another, or leave out `--port` to get a random free one.

## Tests

`python -m unittest discover -s tests -v` checks the driving logic, the phone controller and the
brick setup script without a robot.
