"""Offline tests for ev3_config.py: choosing this laptop's brick.

Run from the project folder:   python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ev3_config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = os.path.join(tmp.name, "ev3_config.json")
        patcher = mock.patch.object(ev3_config, "CONFIG_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_brick_names_become_addresses(self):
        self.assertEqual(ev3_config.brick_address("ev3kishan"), "ev3kishan.local")
        self.assertEqual(ev3_config.brick_address(" ev3kk.local "), "ev3kk.local")
        self.assertEqual(ev3_config.brick_address("192.168.0.1"), "192.168.0.1")

    def test_save_host_keeps_the_login(self):
        with open(self.file, "w") as f:
            json.dump({"host": "ev3kk.local", "user": "robot", "password": "secret1"}, f)
        ev3_config.save_host("ev3kishan.local")
        with open(self.file) as f:
            self.assertEqual(json.load(f), {"host": "ev3kishan.local", "user": "robot", "password": "secret1"})
        with mock.patch.object(sys, "argv", ["ev3_drive.pyw"]):
            self.assertEqual(ev3_config.load()["host"], "ev3kishan.local")

    def test_save_host_without_a_file_uses_defaults(self):
        ev3_config.save_host("ev3kishan.local")
        with open(self.file) as f:
            self.assertEqual(json.load(f), {**ev3_config.DEFAULTS, "host": "ev3kishan.local"})


if __name__ == "__main__":
    unittest.main()
