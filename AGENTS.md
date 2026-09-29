# EV3 Dashboard — notes for coding agents

PC-side tools for a LEGO Mindstorms EV3 brick running **ev3dev** (Debian Linux on the brick).
Everything talks to the brick over **SSH** (`paramiko`) by reading and writing ev3dev's sysfs
files. Nothing is installed on the brick. Python 3 standard library + `paramiko` only; the UIs
are hand-drawn `tkinter`. No build step.

## Files

| File | What it is |
|---|---|
| `ev3_drive.pyw` | **EV3 RC**: drive a two-motor robot from the keyboard like an RC car, with a live dashboard (speedometer, wheel meters, trip, battery, latency) and a calibration card. Holds all the shared drive logic. |
| `ev3_phone.py` + `ev3_phone.html` | Phone controller: a small HTTP server on the PC serving a touch page. Each run `start_server()` opens it on a random free port in `PORT_RANGE` (8000–8999), or on `--port N` (`take_port_arg()` removes that before `ev3_config` reads the brick address). `Server.allow_reuse_address` is off on Windows, where it would let two copies share a port. **Imports `ev3_drive.pyw`** (`SourceFileLoader`) and reuses its `Brick`, `wheel_commands`, `send_drive`, constants and settings. |
| `ev3_widget.pyw` | **EV3 Status** widget: always-on-top window showing battery, CPU/RAM, ports, motors, sensors; jog motors, switch sensor modes, free memory (sudo), stop the running program; Sound card (play built-in/uploaded WAVs, text-to-speech, volume, upload). Independent of the other files. |
| `ev3_sound.py` | Sound commands, sound-list query/parsing and WAV upload, shared by the drive app and the widget (each builds its own Sound card UI). |
| `ev3_config.py` | Loads `ev3_config.json` (brick address + SSH login) for all three apps. Each laptop has its own (git-ignored) and drives its own brick: `python ev3_config.py <name or IP>` saves the host (`brick_address()` adds `.local` to bare names; `save_host()` keeps the login). |
| `tests/test_config.py` | Offline tests for `ev3_config.py` (temp config file). |
| `install.bat` / `install.sh` | Per-laptop setup: Python (winget on Windows, if missing), `pip install -r requirements.txt`, tkinter check, `ev3_config.py <brick>`, optional desktop shortcut (Windows). Safe to re-run. `.gitattributes` keeps `.bat` CRLF and `.sh` LF. |
| `programs/<name>/` | EV3 MicroPython programs that run on the brick (VS Code LEGO EV3 MicroPython extension projects: `main.py` with a `pybricks-micropython` shebang, `.vscode/launch.json` for "Download and Run"). Their `.vscode` folders are committed (`.gitignore` exception). Not run on the PC; not covered by the tests. |
| `ev3_setup.py` | Copies the EV3 program folders in `programs/` (`programs/<name>/main.py`) to `/home/<user>/<name>` on a brick over SFTP, like "Download and Run" (skips dotfiles/caches, `chmod 755` on `#!` scripts), then prints read-only brick facts. Used to make several bricks identical. Never deletes on the brick, never moves motors. `take_args()` strips `--dry-run` / `--programs DIR` before `ev3_config` reads the address. |
| `tests/test_drive.py` | Offline tests for the drive logic (fake brick, fake clock). |
| `tests/test_setup.py` | Offline tests for `ev3_setup.py` (temp program folders, fake SFTP). |
| `ev3_drive_settings.json` | Created at runtime, git-ignored. Motor ports, per-wheel invert, drift trim, last gear. Written by the desktop app, read (and `mode` written) by the phone server. |

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

1. `wheel_commands(held, mode, trim)` → target wheel speeds in deg/s, **wheel space** (+ = forward,
   before Invert). Curves slow the inner wheel to `TURN_INNER`; left/right alone spins in place.
2. `send_drive(brick, lp, rp, left, right, mode, invert_left, invert_right)`:
   - `Brick.ramp()` moves from the last levels toward the targets. **Overall speed** (average of the
     wheels) changes over `RAMP_SECONDS`; **steering** (half their difference) over `STEER_SECONDS`,
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
`Brick.horn()` (H key, HORN button, phone `/horn`) skips the horn if `aplay` is already running,
because a held key repeats. In EV3 RC, key events from the Say text box are ignored by the drive
key handlers, so typing never drives the robot.

## Rules that must hold

- **Every way of moving a motor must stop by itself** if the PC app dies, the window loses focus,
  or Wi-Fi/SSH drops: timed pulses, or the brick-side watchdog for `run-direct`. Never add a
  `run-forever` / `run-direct` path without that cover.
- **Do not move the real robot on your own.** It may be on a desk. Read-only SSH checks (listing
  motors, battery) are fine; ask the user before sending anything that turns a motor, and let them
  do the driving tests.
- `ev3_phone.py` depends on `ev3_drive.pyw`'s names and signatures (`Brick`, `wheel_commands`,
  `send_drive`, `load_settings`/`save_settings`, `MODES`, `SETTINGS_FILE`, `DEFAULT_LEFT/RIGHT`,
  `BATTERY_RANGE`, `AA_RANGE`, `MAX_SPEED`, `MOTOR_LIMIT`, `MONITOR_SECONDS`, `deg_to_cm`, `HOST`).
  Change both files together, and keep the phone driving exactly like the desktop.
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
- Tests or scripts that create `DriveApp` save `ev3_drive_settings.json` (the user's real
  calibration). Patch `save_settings` too, or back the file up first.
- Key releases in `ev3_drive.pyw` are delayed by `KEY_RELEASE_MS` and cancelled by an immediate
  press: macOS/Linux Tk auto-repeat sends release+press pairs, which would otherwise brake and
  restart the ramp many times a second. Windows only repeats presses.
- Match the existing style: short docstrings saying why, not what; no new dependencies.

## Checking a change

1. `python -m unittest discover -s tests -v`. Add a test there for any drive-logic change.
2. For UI changes, launch the app (`python ev3_drive.pyw`); it runs without a robot and shows
   "Connecting…".
3. Hand the real-robot check to the user with concrete steps (what to press, what should happen).
