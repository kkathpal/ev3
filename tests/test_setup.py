"""Offline tests for ev3_setup.py: which files go to the brick, and how. No brick needed.

Run from the project folder:   python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
with mock.patch.object(sys, "argv", ["ev3_setup.py"]):
    import ev3_setup as setup


class FakeSFTP:
    """Records what would happen on the brick."""

    def __init__(self):
        self.dirs, self.files, self.modes = [], {}, {}

    def mkdir(self, path):
        if path in self.dirs:
            raise OSError("exists")
        self.dirs.append(path)

    def put(self, local, remote):
        assert remote.rsplit("/", 1)[0] in self.dirs, f"folder missing for {remote}"
        with open(local, "rb") as f:
            self.files[remote] = f.read()

    def chmod(self, remote, mode):
        self.modes[remote] = mode


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name

        def write(path, data=b"x"):
            full = os.path.join(root, *path.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)

        write("car/main.py", b"#!/usr/bin/env pybricks-micropython\nprint(1)\n")
        write("car/lib/helpers.py", b"def f(): pass\n")
        write("car/.vscode/launch.json", b"{}")
        write("car/__pycache__/helpers.cpython-38.pyc")
        write("car/.gitignore")
        write("arm/main.py", b"#!/usr/bin/env pybricks-micropython\n")
        write("arm/sounds/beep.wav", b"RIFF")
        write("notes/readme.txt")          # no main.py: not a program
        write(".hidden/main.py")           # hidden folder: skipped
        self.root = root

    def test_finds_program_folders_only(self):
        names = [os.path.basename(p) for p in setup.find_programs(self.root)]
        self.assertEqual(names, ["arm", "car"])

    def test_copies_like_download_and_run(self):
        files, dirs = setup.plan(setup.find_programs(self.root), "/home/robot")
        sftp = FakeSFTP()
        setup.copy(sftp, files, dirs)
        self.assertEqual(sorted(sftp.files), [
            "/home/robot/arm/main.py", "/home/robot/arm/sounds/beep.wav",
            "/home/robot/car/lib/helpers.py", "/home/robot/car/main.py"])   # no .vscode, caches, dotfiles
        self.assertEqual(sftp.modes, {"/home/robot/arm/main.py": 0o755, "/home/robot/car/main.py": 0o755})
        setup.copy(sftp, files, dirs)   # running it again (folders exist) is fine

    def test_reads_brick_facts(self):
        facts = setup.parse_check("name ev3kishan\nbattery 7912000\nmotor outA\nmotor outD\nfree_kb 812345\n")
        self.assertEqual(facts, {"name": "ev3kishan", "battery": "7912000", "motors": ["outA", "outD"],
                                 "free_kb": "812345"})

    def test_command_line_flags_leave_the_address(self):
        with mock.patch.object(sys, "argv", ["ev3_setup.py", "192.168.0.1", "--dry-run", "--programs", "D:/ev3"]):
            self.assertEqual(setup.take_args(), (True, "D:/ev3"))
            self.assertEqual(sys.argv, ["ev3_setup.py", "192.168.0.1"])


if __name__ == "__main__":
    unittest.main()
