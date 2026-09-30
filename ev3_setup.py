"""Set up an EV3 brick with your programs, so several bricks can be made identical.

Copies every EV3 program folder in programs/ (folders like programs/brick1_test with a
main.py, made with the VS Code EV3 extension) to /home/robot/<folder> on the brick, the same way
the extension's "Download and Run" does: hidden files (.vscode, ...) and Python caches are
skipped, and main.py is made runnable. Then it checks the brick: name, battery, motors,
free space. It never moves a motor, and never deletes anything on the brick (files your
programs saved there, like data logs, stay).

Run:   python ev3_setup.py                      (brick address from ev3_config.json)
       python ev3_setup.py 192.168.0.1          (brick at a specific address)
       python ev3_setup.py ev3kishan.local --dry-run   (only list what would be copied)
       python ev3_setup.py 192.168.0.1 --programs D:\\ev3   (programs from another folder)
Run it once per brick to make them all the same.
"""
import os
import sys


def take_args():
    """Remove --dry-run and --programs DIR from the command line (ev3_config reads the
    first argument as the brick's address) and return (dry_run, programs_dir)."""
    args = sys.argv
    dry_run = "--dry-run" in args
    if dry_run:
        args.remove("--dry-run")
    programs = None
    if "--programs" in args:
        i = args.index("--programs")
        if i + 1 >= len(args):
            sys.exit("Use:  python ev3_setup.py [brick-address] --programs FOLDER")
        programs = args[i + 1]
        del args[i:i + 2]
    return dry_run, programs


DRY_RUN, PROGRAMS_ARG = take_args()

import ev3_config   # noqa: E402  (after take_args, which cleans the command line for it)

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAMS_DIR = os.path.join(HERE, "programs")   # the EV3 programs, one folder each
SKIP_DIRS = {"__pycache__", ".venv", "venv"}
SKIP_SUFFIXES = (".pyc", ".pyo")

# Read-only facts about the brick, one "key value" line each (shell builtins only)
CHECK = r"""
read name < /proc/sys/kernel/hostname; echo "name $name"
read v < /sys/class/power_supply/lego-ev3-battery/voltage_now; echo "battery $v"
for m in /sys/class/tacho-motor/motor*; do [ -d "$m" ] && read a < $m/address && echo "motor ${a##*:}"; done
df -k /home | tail -1 | while read _ _ _ free _; do echo "free_kb $free"; done
"""


def is_program(path):
    """An EV3 program folder: has a main.py (the file "Download and Run" starts)."""
    return os.path.isfile(os.path.join(path, "main.py"))


def find_programs(root):
    """EV3 program folders directly inside `root`."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [os.path.join(root, n) for n in names if not n.startswith(".") and is_program(os.path.join(root, n))]


def plan(programs, remote_home):
    """What to copy: [(local file, brick path, runnable?)] and the brick folders to make."""
    files, dirs = [], []
    for program in programs:
        base = f"{remote_home}/{os.path.basename(program)}"
        for folder, subdirs, names in os.walk(program):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith(".") and d not in SKIP_DIRS)
            rel = os.path.relpath(folder, program).replace(os.sep, "/")
            remote_dir = base if rel == "." else f"{base}/{rel}"
            dirs.append(remote_dir)
            for name in sorted(names):
                if name.startswith(".") or name.endswith(SKIP_SUFFIXES):
                    continue
                local = os.path.join(folder, name)
                with open(local, "rb") as f:
                    runnable = f.read(2) == b"#!"   # a script with a #! line, like main.py
                files.append((local, f"{remote_dir}/{name}", runnable))
    return files, dirs


def copy(sftp, files, dirs):
    """Make the folders and copy the files over an open SFTP session."""
    for d in dirs:
        try:
            sftp.mkdir(d)
        except OSError:
            pass   # already there
    for local, remote, runnable in files:
        sftp.put(local, remote)
        if runnable:
            sftp.chmod(remote, 0o755)


def parse_check(text):
    facts = {"motors": []}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "motor":
            facts["motors"].append(value)
        elif key:
            facts[key] = value
    return facts


def main():
    config = ev3_config.load()
    root = PROGRAMS_ARG or PROGRAMS_DIR
    programs = find_programs(root)
    if not programs:
        sys.exit(f"No EV3 programs found in {root} (looking for folders with a main.py).")
    remote_home = f"/home/{config['user']}"
    files, dirs = plan(programs, remote_home)
    print(f"Programs from {root}: {', '.join(os.path.basename(p) for p in programs)}"
          f" ({len(files)} files)")
    if DRY_RUN:
        for local, remote, runnable in files:
            print(f"  {remote}" + ("   (runnable)" if runnable else ""))
        return

    import paramiko   # only needed for the real copy
    host = config["host"]
    print(f"Connecting to {host}…")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ev3_config.ssh_address(host), username=config["user"], password=config["password"], timeout=10,
                       look_for_keys=False, allow_agent=False)
    except Exception as e:
        sys.exit(f"Can't connect to {host}: {e}\nCheck the brick is on (its screen shows an IP address) "
                 f"and connected, then give that IP:  python ev3_setup.py <IP>")
    try:
        _, out, _ = client.exec_command(CHECK, timeout=20)
        facts = parse_check(out.read().decode(errors="replace"))
        print(f"Brick: {facts.get('name', '?')}")
        sftp = client.open_sftp()
        try:
            copy(sftp, files, dirs)
        finally:
            sftp.close()
        print(f"Copied {len(files)} files to {remote_home}/ "
              f"({', '.join(os.path.basename(p) for p in programs)})")
    finally:
        client.close()

    volts = int(facts["battery"]) / 1e6 if facts.get("battery", "").isdigit() else None
    free_mb = int(facts["free_kb"]) // 1024 if facts.get("free_kb", "").isdigit() else None
    print(f"  battery: {volts:.1f} V" if volts else "  battery: ?")
    print(f"  motors:  {', '.join(sorted(facts['motors'])) or 'none plugged in'}")
    print(f"  free space: {free_mb} MB" if free_mb is not None else "  free space: ?")
    print("Done. On the brick: File Browser → the program folder → main.py to run it.")


if __name__ == "__main__":
    main()
