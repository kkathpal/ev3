# EV3 Dashboard — notes for coding agents

PC-side tools for a LEGO Mindstorms EV3 brick running **ev3dev** (Debian Linux on the brick).
Everything talks to the brick over **SSH** (`paramiko`) by reading and writing ev3dev's sysfs
files. Nothing is installed on the brick. Python 3 standard library + `paramiko` only; the UIs
are hand-drawn `tkinter`. No build step.

## Files

| File | What it is |
|---|---|
| `ev3_drive.pyw` | **EV3 RC**: drive a two-motor robot from the keyboard like an RC car, with a live dashboard (speedometer, wheel meters, trip, battery, latency) and a calibration card. Holds all the shared drive logic. |
| `ev3_phone.py` + `ev3_phone.html` | Phone controller: a small HTTP server on the PC serving a touch page (a joystick with STOP and HORN under it, gears), with a Setup · Calibrate screen mirroring the desktop's Setup card (`/setup` saves motors/Invert/trim/accel; `/cal/test` runs a `CAL_PATTERNS` test while the phone keeps re-sending it, stopped by `PHONE_TIMEOUT`; `/cal/save` applies `cal_result()`), and a Connection screen: `/connect` sets `rc.HOST` (the monitor then `close()`s the old brick and connects the new one; `brick_host` tracks which address the live connection used) and can `save_host()`; `/scan` runs `find_bricks(report)` in the background: port 22 on the PC's private networks (Bluetooth/USB links first, as `LINK_PREFIX` /24 with the `.1` address tried first; then the Wi-Fi `SCAN_PREFIX` /22), keeping Debian OpenSSH greetings, each labelled `via` and reported as soon as found (names from reverse lookup fill in after), never logging in; plus `paired_bluetooth_bricks()` (Windows) for paired bricks whose Bluetooth network isn't connected. The page opens the Connection screen and starts a search on every load. `/drive` takes `{"stick": [x, y], "mode": …}` from the joystick (x right, y forward, -1..1; re-sent every ~100 ms while touched) or `{"held": [...], "mode": …}` from a desktop browser's arrow keys; `parse_stick()` rejects non-numbers/NaN/Infinity and clamps, stick wins over held, and `stop()`/`cal_test()` clear it. `_check_ports()` (in `_monitor` after each `read_motors`, keyed on `known_ports`) picks the motor pair like the desktop when the brick's motors change: `pick_motors()` on the saved ports and, if that differs, `_save({"left", "right"})` (stops first), so both apps agree; `rc.MotorsChanged` → `brick.drop()` + `continue` (reconnects at once: no "Brick disconnected" status or 2 s wait, the phone keeps its last telemetry meanwhile); the page's Setup sheet rebuilds its motor `<select>`s when `setup.ports` changes (`buildPorts`/`portIds`). Each run `start_server()` opens it on a random free port in `PORT_RANGE` (8000–8999), or on `--port N` (`take_port_arg()` removes that before `ev3_config` reads the brick address). `Server.allow_reuse_address` is off on Windows, where it would let two copies share a port. **Imports `ev3_drive.pyw`** (`SourceFileLoader`) and reuses its `Brick`, `wheel_commands`, `stick_commands`, `send_drive`, constants and settings. |
| `ev3_widget.pyw` | **EV3 Status** widget: always-on-top window showing battery, CPU/RAM, ports, motors, sensors; jog motors, switch sensor modes, free memory (sudo), stop the running program; Sound card (play built-in/uploaded WAVs, text-to-speech, volume, upload). Independent of the other files. |
| `ev3_sound.py` | Sound commands, sound-list query/parsing and WAV upload, shared by the drive app and the widget (each builds its own Sound card UI). |
| `ev3_config.py` | Loads `ev3_config.json` (brick address + SSH login) for all three apps. Each laptop has its own (git-ignored) and drives its own brick: `python ev3_config.py <name or IP>` saves the host (`brick_address()` adds `.local` to bare names; `save_host()` keeps the login). Every SSH connect goes through `ssh_address(host)`: the brick's first IPv4 address, because Windows can list a `.local` name's IPv6 link-local address first, ev3dev's SSH doesn't answer there, and paramiko gives up after that one timeout. |
| `tests/test_config.py` | Offline tests for `ev3_config.py` (temp config file). |
| `install.bat` / `install.sh` | Per-laptop setup: Python (winget on Windows, if missing), `pip install -r requirements.txt`, tkinter check, `ev3_config.py <brick>`, optional desktop shortcut (Windows). Safe to re-run. `.gitattributes` keeps `.bat` CRLF and `.sh` LF. |
| `programs/<name>/` | EV3 MicroPython programs that run on the brick (VS Code LEGO EV3 MicroPython extension projects: `main.py` with a `pybricks-micropython` shebang, `.vscode/launch.json` for "Download and Run"). Their `.vscode` folders are committed (`.gitignore` exception). Not run on the PC; not covered by the tests. |
| `ev3_setup.py` | Copies the EV3 program folders in `programs/` (`programs/<name>/main.py`) to `/home/<user>/<name>` on a brick over SFTP, like "Download and Run" (skips dotfiles/caches, `chmod 755` on `#!` scripts), then prints read-only brick facts. Used to make several bricks identical. Never deletes on the brick, never moves motors. `take_args()` strips `--dry-run` / `--programs DIR` before `ev3_config` reads the address. |
| `tests/test_drive.py` | Offline tests for the drive logic (fake brick, fake clock). |
| `tests/test_phone.py` | Offline tests for `ev3_phone.py` (`Controller`, `parse_stick`; fake brick). |
| `tests/test_setup.py` | Offline tests for `ev3_setup.py` (temp program folders, fake SFTP). |
| `ev3_drive_settings.json` | Created at runtime, git-ignored. Motor ports, per-wheel invert, drift trim, last gear. Written by the desktop app and by the phone's Setup screen (`/setup`, `/cal/save`), and read by both; the phone also writes `mode`, and `left`/`right` when `_check_ports()` auto-picks the pair. Keys: `left`, `right`, `invert_left`, `invert_right`, `trim`, `mode`, `accel`. |

`.pyw` = Python run without a console on Windows. Run with `python` (not `pythonw`) to see tracebacks.

## Running

```
pip install -r requirements.txt
python ev3_drive.pyw [brick-address]     # address overrides ev3_config.json
python ev3_phone.py  [brick-address] [--port N]   # then open the printed http://<PC>:<port> on a phone
python ev3_widget.pyw [brick-address]
python ev3_setup.py [brick-address] [--dry-run] [--programs DIR]   # copy EV3 programs to a brick
python -m unittest discover -s tests -v  # offline, no robot needed
```

The brick's address and SSH login come from `ev3_config.json` (git-ignored, created with ev3dev's
defaults `ev3dev.local` / `robot` / `maker` on first run) via `ev3_config.load()`, which each app
turns into its `HOST`, `USER`, `PASSWORD` constants. A command-line address overrides the file.
Never hard-code an address or password in the apps.

## How driving works (`ev3_drive.pyw`)

Pipeline, called every `RENEW_MS` (120 ms) while a key is held, and immediately on any key change:

1. `wheel_commands(held, mode, trim)` = `drift_fix(*arrow_speeds(held, mode), trim)` → target wheel
   speeds in deg/s, **wheel space** (+ = forward, before Invert). Curves slow the inner wheel to
   `TURN_INNER`; left/right alone spins in place. The phone's joystick goes through
   `stick_commands(x, y, mode, trim)` instead (x right, y forward, -1..1): nothing inside
   `STICK_DEADZONE`, speed ∝ push beyond it, direction a linear blend of the two nearest of the
   arrows' eight moves (`STICK_MOVES`), so the eight arrow directions drive exactly like the keys
   and nothing jumps around the circle. Both feed the same `send_drive`.
2. `send_drive(brick, lp, rp, left, right, mode, invert_left, invert_right, ramp)`:
   - `Brick.ramp()` moves from the last levels toward the targets. **Overall speed** (average of the
     wheels) changes over `ramp` seconds, the saved Acceleration setting (`ACCELERATIONS`, key `accel`,
     read with `ramp_seconds(settings)`; default `RAMP_SECONDS` = Quick); **steering** (half their difference) over `STEER_SECONDS`,
     scaled with speed so a curve keeps its shape from a standstill. Rates are per second (from
     `time.monotonic()`), not per command, because the phone renews faster than the desktop.
     This protects the robot's gears; don't remove it.
   - Invert is applied after ramping (motor space = wheel space × ±1).
   - Normal gears: `Brick.drive()` → `speed_sp` + `time_sp=PULSE_MS` + `run-timed` (speed-regulated,
     expires on its own). While the `CalibrateWindow` is open, `_tick` instead sends that window's
     raw `CAL_PATTERNS` at `TEST_SPEED` straight to `Brick.drive()` (no Invert, no ramp). Turbo: `Brick.drive_direct()` → `duty_cycle_sp` + `run-direct` (raw power,
     no timeout) plus a heartbeat write to `$HB`.
3. Key release → `Brick.stop(released=True)` uses `RELEASE_STOP`; Space / STOP button / focus loss /
   phone timeout → `Brick.stop()` uses `HARD_STOP`. Stop actions: `coast` < `brake` < `hold`.
   `stop()` stops every motor, not just the selected pair.

`Brick` keeps two long-lived shells over one SSH connection: `ctl` (drive commands, one line of
`echo … > /sys/class/tacho-motor/motorN/…` per call, never waits for output) and `mon` (readback via
a `mon` shell function, every `MONITOR_SECONDS`). `watchdog_script()` starts a background loop on the
brick that stops all motors if the Turbo heartbeat goes stale for `WATCHDOG_CS` or the SSH session dies.
`read_motors()` raises `MotorsChanged` (a `ConnectionError`) when the brick's motor set no longer
matches `paths` (a cable moved). Both apps' `_monitor` answer it with `Brick.drop()` + `continue`:
the client is closed without a stop command (its shells and watchdog end with it on the brick, and
the watchdog still stops a Turbo run) and `client`/`ctl`/`mon`/`mon_out` are cleared, so the next
pass `connect()`s at once with a watchdog for the new motors, instead of the 2 s disconnected pause
that used to leave the old shells running. The pair is then re-picked with
`pick_motors(left, right, ports)`: the saved picks when the brick has them, otherwise motors it does
have, with fewer than two motors left alone (desktop `_fill_port_menus`, called from `_refresh_ui`
when `paths` differ from `known_ports`; phone `_check_ports()`); both save the result.

**Calibrate…** (`CalibrateWindow`): each arrow runs the picked motors in a fixed raw pattern and the
user clicks what the robot did. `cal_result()` turns the answers into swap + invert_left/right, so
calibration still ends up as the same settings keys (and the phone needs no change); `cal_predict()`
pre-fills the answers from the current Invert settings. The window routes its keys through the app's
`_key_down`/`_key_up`, so auto-repeat, focus loss and Space behave exactly like driving.

Gears live in `MODES` (name, % of `MAX_SPEED`). ev3dev rejects `speed_sp` above the motor's
`max_speed`, hence `MOTOR_LIMIT` (1050) as the clamp and the full-power scale.

## Sound

The brick has `aplay`, `espeak`, `beep` and `amixer` (mixer control `PCM`); no MP3 player.
Built-in effects are WAVs under `/usr/share/sounds/ev3dev/<group>/`; uploads go over SFTP to
`~/sounds`. All of this lives in `ev3_sound.py`: `QUERY` (prints `vol=` and `sounds=`, run by the
widget's `info()` and by `Brick.query_sounds()` after connecting), `play`/`say`/`STOP`/`set_volume`/
`horn` command strings, and `upload()`. Sound commands go to the same command shell as motor
commands, so they always run in the background (`( … ) &`) or they would delay driving.
`Brick.horn()` (H key, the HORN on the D-pad and in the Sound card, phone `/horn`) honks at most once per `HORN_GAP`, because a held
key repeats; the limit is on the PC, not a `pgrep aplay` check on the brick (a player that never
exited blocked every horn after the first). `ev3_sound.horn()` wraps aplay in `timeout`. In EV3 RC, key events from the Say text box are ignored by the drive
key handlers, so typing never drives the robot.

## Rules that must hold

- **Every way of moving a motor must stop by itself** if the PC app dies, the window loses focus,
  or Wi-Fi/SSH drops: timed pulses, or the brick-side watchdog for `run-direct`. Never add a
  `run-forever` / `run-direct` path without that cover.
- **Do not move the real robot on your own.** It may be on a desk. Read-only SSH checks (listing
  motors, battery) are fine; ask the user before sending anything that turns a motor, and let them
  do the driving tests.
- `ev3_phone.py` depends on `ev3_drive.pyw`'s names and signatures (`Brick`, `MotorsChanged`,
  `wheel_commands`, `stick_commands`, `send_drive`, `ramp_seconds`, `pick_motors`, `port_name`,
  `cal_result`/`cal_predict`, `load_settings`/`save_settings`, `MODES`, `ACCELERATIONS`,
  `DEFAULT_ACCEL`, `CAL_PATTERNS`, `TEST_SPEED`, `TEST_MS`, `TRIM_RANGE`, `SETTINGS_FILE`,
  `DEFAULT_LEFT/RIGHT`, `BATTERY_RANGE`, `AA_RANGE`, `MAX_SPEED`, `MOTOR_LIMIT`, `MONITOR_SECONDS`,
  `deg_to_cm`, `HOST`). Change both files together, and keep the phone driving exactly like the
  desktop (the joystick's eight arrow directions must equal the arrow keys' moves).
- Keep the settings JSON keys backward compatible (`reverse` is an older single-invert key still read).
- The brick's CPU is slow. Anything that polls it uses shell builtins only (`read`, `echo`, `case`),
  runs `renice -n 19`, and avoids starting processes per poll.

## Conventions

- Sizes go through `px()` (scaled from screen DPI); colours are the module constants (`BG`, `CARD`,
  `ACCENT`, …); fonts are the `FONT_*` constants (Windows fonts; other OSes fall back).
- Tunables are the UPPER_CASE constants at the top of each file, each with a short comment.
- Windows-only calls (`ctypes.windll`, DWM title bar, DPI awareness) stay inside `try/except` so
  macOS and Linux still run. On macOS the apps set `tk scaling` so sizes match Windows, and the
  widget's menu is on Button-2 / Control-click instead of Button-3.
- Windows must fit a 1920×1080 screen at 150% scaling (about 720 logical px tall): cards can be
  folded by clicking their title, and `_fit_screen()` folds the least-needed ones at startup
  (drive app: Setup then Sound, or Sound first on a first run; widget: Activity then System).
  Keep new UI inside a foldable card, and check the height after adding anything.
- The widget is borderless (`overrideredirect`) on Windows only; elsewhere that would block
  keyboard focus. A missing `paramiko` shows a message box, since `.pyw` files have no console.
- Tests or scripts that create `DriveApp` or the phone's `Controller` save `ev3_drive_settings.json`
  (the user's real calibration). Patch `save_settings` (and `Controller._reload_settings`, as
  `tests/test_phone.py` does), or back the file up first.
- Key releases in `ev3_drive.pyw` are delayed by `KEY_RELEASE_MS` and cancelled by an immediate
  press: macOS/Linux Tk auto-repeat sends release+press pairs, which would otherwise brake and
  restart the ramp many times a second. Windows only repeats presses.
- Match the existing style: short docstrings saying why, not what; no new dependencies.

## Checking a change

1. `python -m unittest discover -s tests -v`. Add a test there for any drive-logic change.
2. For UI changes, launch the app (`python ev3_drive.pyw`); it runs without a robot and shows
   "Connecting…".
3. Hand the real-robot check to the user with concrete steps (what to press, what should happen).
