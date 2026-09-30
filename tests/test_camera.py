"""Offline tests for the camera snapshots in ev3_camera.py: no brick, no webcam, no SSH.

Run from the project folder:   python -m unittest discover -s tests -v
"""
import base64
import io
import os
import queue
import socket
import struct
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ev3_camera as cam

JPEG1, JPEG2 = b"\xff\xd8 one", b"\xff\xd8 two"


def framed(kind, data):
    return kind + struct.pack(">I", len(data)) + data


class FakeSession:
    """What open_session() returns, faked: recv() hands out what the test put in, raises
    socket.timeout while there's nothing (a live but quiet script), and returns b"" once the test
    says the stream ended; fetch() returns the "file on the brick" the test set (or raises it)."""

    def __init__(self):
        self.chunks = queue.Queue()
        self.closed = False
        self.timeout = None
        self.file = None
        self.fetches = 0

    def snapshot(self, jpeg):
        """The brick saved `jpeg` and announced it."""
        self.file = jpeg
        self.chunks.put(framed(b"N", str(len(jpeg)).encode()))

    def send(self, kind, data):
        self.chunks.put(framed(kind, data))

    def end(self):
        self.chunks.put(b"")

    def settimeout(self, seconds):
        self.timeout = seconds

    def recv(self, n):
        try:
            chunk = self.chunks.get(timeout=0.02)
        except queue.Empty:
            raise socket.timeout()
        if chunk == b"":
            self.chunks.put(b"")   # a closed channel keeps returning EOF
            return b""
        head, rest = chunk[:n], chunk[n:]
        if rest:
            self.chunks.queue.appendleft(rest)
        return head

    def fetch(self):
        self.fetches += 1
        if isinstance(self.file, Exception):
            raise self.file
        return self.file

    def close(self):
        self.closed = True


def wait_until(check, seconds=1.0):
    end = time.monotonic() + seconds
    while not check():
        if time.monotonic() > end:
            return False
        time.sleep(0.01)
    return True


class ReadMessageTest(unittest.TestCase):

    @staticmethod
    def reader(data):
        buf = io.BytesIO(data)

        def read(n):
            chunk = buf.read(n)
            if len(chunk) < n:
                raise EOFError("closed")
            return chunk
        return read

    def test_notices_and_errors(self):
        read = self.reader(framed(b"N", b"1234") + framed(b"E", b"oops") + framed(b"N", b""))
        self.assertEqual(cam.read_message(read), (b"N", b"1234"))
        self.assertEqual(cam.read_message(read), (b"E", b"oops"))
        self.assertEqual(cam.read_message(read), (b"N", b""))

    def test_oversize_is_rejected(self):
        read = self.reader(b"N" + struct.pack(">I", cam.MAX_FRAME + 1))
        with self.assertRaises(ValueError):
            cam.read_message(read)

    def test_eof_propagates(self):
        with self.assertRaises(EOFError):
            cam.read_message(self.reader(b"N\0\0"))
        with self.assertRaises(EOFError):
            cam.read_message(self.reader(framed(b"E", b"oops")[:-2]))


class CameraTest(unittest.TestCase):

    def setUp(self):
        patches = [mock.patch.object(cam, "LINGER_SECONDS", 0.1),
                   mock.patch.object(cam, "FRAME_TIMEOUT", 0.3)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.session = FakeSession()
        self.camera = cam.Camera(lambda: self.session)

    def stopped(self):
        return wait_until(lambda: not self.camera.state()["live"] and not self.camera.running)

    def newer(self, seq):
        return wait_until(lambda: self.camera.seq > seq)

    def test_snapshots_reach_the_viewer(self):
        with self.camera.viewing():
            self.assertEqual(self.camera.state()["status"], "Starting…")
            self.assertIsNone(self.camera.state()["age"])
            self.session.snapshot(JPEG1)
            self.assertEqual(self.camera.latest(timeout=1), JPEG1)   # waited for the first one
            state = self.camera.state()
            self.assertEqual(state["status"], "Live")
            self.assertTrue(state["live"])
            self.assertEqual(state["interval"], cam.SNAP_SECONDS)
            self.assertLess(state["age"], 1)
            self.assertEqual(self.session.timeout, 1.0)
            self.session.snapshot(JPEG2)
            self.assertTrue(self.newer(1))
            self.assertEqual(self.camera.latest(), JPEG2)   # returns at once now
            self.assertEqual(self.session.fetches, 2)
            self.session.end()
            self.assertTrue(self.stopped())
            self.assertEqual(self.camera.state()["status"], "The brick closed the camera stream.")
        self.assertTrue(self.session.closed)

    def test_latest_times_out_without_a_snapshot(self):
        with self.camera.viewing():
            t = time.monotonic()
            self.assertIsNone(self.camera.latest(timeout=0.05))
            self.assertLess(time.monotonic() - t, 0.25)
            self.assertTrue(self.camera.running)

    def test_error_message_sets_status(self):
        with self.camera.viewing():
            self.session.send(b"E", "Camera refused 320x240 MJPEG: Invalid argument".encode())
            self.assertIsNone(self.camera.latest(timeout=1))   # ended without a picture
            self.assertEqual(self.camera.state()["status"],
                             "Camera refused 320x240 MJPEG: Invalid argument")
            self.assertFalse(self.camera.state()["live"])
            self.assertFalse(self.camera.running)
        self.assertTrue(self.session.closed)

    def test_failed_fetch_is_reported_and_capture_goes_on(self):
        with self.camera.viewing():
            self.session.file = OSError("No such file")
            self.session.send(b"N", b"")
            self.assertTrue(wait_until(lambda: self.camera.state()["status"].startswith("Couldn't")))
            self.assertEqual(self.camera.state()["status"], "Couldn't fetch the snapshot: No such file")
            self.assertTrue(self.camera.running)
            self.session.file = b"<html>not a jpeg"   # wrong content
            self.session.send(b"N", b"16")
            self.assertTrue(wait_until(
                lambda: self.camera.state()["status"] == "The snapshot on the brick isn't a JPEG."))
            self.assertTrue(self.camera.running)
            self.session.snapshot(JPEG1)   # the next good one recovers
            self.assertEqual(self.camera.latest(timeout=1), JPEG1)
            self.assertEqual(self.camera.state()["status"], "Live")
            self.session.send(b"X", b"?")   # unknown kinds are ignored
            self.session.snapshot(JPEG2)
            self.assertTrue(self.newer(1))
            self.assertEqual(self.camera.latest(), JPEG2)

    def test_missing_brick(self):
        camera = cam.Camera(lambda: None)
        with camera.viewing():
            self.assertIsNone(camera.latest(timeout=1))
            self.assertEqual(camera.state(), {"status": "Brick not connected", "live": False,
                                              "interval": cam.SNAP_SECONDS, "age": None})
        self.assertFalse(camera.running)

    def test_stops_after_the_last_viewer_leaves(self):
        with self.camera.viewing():
            self.session.snapshot(JPEG1)
            self.assertEqual(self.camera.latest(timeout=1), JPEG1)
            with self.camera.viewing():
                self.assertEqual(self.camera.viewers, 2)
            self.session.snapshot(JPEG2)   # one viewer left: still capturing
            self.assertTrue(self.newer(1))
            self.assertEqual(self.camera.latest(), JPEG2)
            self.assertFalse(self.session.closed)
        self.assertTrue(self.stopped())
        self.assertEqual(self.camera.state()["status"], "Off")
        self.assertTrue(self.session.closed)

    def test_stalled_camera(self):
        with self.camera.viewing():
            self.assertIsNone(self.camera.latest(timeout=1))   # nothing ever arrives
            self.assertEqual(self.camera.state()["status"], "The camera stopped sending frames.")
        self.assertTrue(self.session.closed)

    def test_restarts_for_a_viewer_who_arrives_as_it_goes_idle(self):
        sessions = []

        def opener():
            sessions.append(FakeSession())
            return sessions[-1]
        camera = cam.Camera(opener)
        with camera.viewing():
            sessions[0].snapshot(JPEG1)
            self.assertEqual(camera.latest(timeout=1), JPEG1)
        # the idle stop is decided, then a viewer arrives before the thread has finished
        with camera.cond:
            camera.viewers += 1
            self.assertTrue(camera._finish("Off"))    # kept running, starts over
            self.assertEqual(camera.status, "Starting…")
            self.assertTrue(camera.running)
            self.assertIsNone(camera.frame)
            camera.viewers -= 1
            self.assertFalse(camera._finish("Off"))
            self.assertFalse(camera.running)
        self.assertTrue(wait_until(lambda: sessions[0].closed))

    def test_a_second_session_starts_after_the_first_ended(self):
        sessions = []

        def opener():
            sessions.append(FakeSession())
            return sessions[-1]
        camera = cam.Camera(opener)
        with camera.viewing():
            sessions[0].end()
            self.assertIsNone(camera.latest(timeout=1))
        self.assertTrue(wait_until(lambda: not camera.running))
        with camera.viewing():
            self.assertTrue(wait_until(lambda: len(sessions) == 2))
            sessions[1].snapshot(JPEG2)
            self.assertEqual(camera.latest(timeout=1), JPEG2)
            sessions[1].end()
        self.assertTrue(wait_until(lambda: not camera.running))


class SessionTest(unittest.TestCase):

    def test_fetch_reads_latest_under_the_users_home(self):
        sftp = mock.Mock()
        sftp.normalize.return_value = "/home/robot"
        sftp.open.return_value.__enter__ = lambda s: s
        sftp.open.return_value.__exit__ = mock.Mock(return_value=False)
        sftp.open.return_value.read.return_value = JPEG1
        session = cam.Session(mock.Mock(), sftp)
        self.assertEqual(session.fetch(), JPEG1)
        sftp.normalize.assert_called_once_with(".")
        sftp.open.assert_called_with("/home/robot/camera/latest.jpg", "rb")
        session.fetch()
        sftp.normalize.assert_called_once()   # the path is remembered
        session.close()
        self.assertTrue(session.channel.close.called)
        self.assertTrue(sftp.close.called)

    def test_open_session_runs_the_script_and_opens_sftp(self):
        client = mock.Mock()
        client.get_transport.return_value.is_active.return_value = True
        channel = client.get_transport.return_value.open_session.return_value
        session = cam.open_session(client)
        channel.exec_command.assert_called_once_with(cam.brick_command())
        self.assertIs(session.sftp, client.open_sftp.return_value)
        self.assertIs(cam.open_channel, cam.open_session)
        client.get_transport.return_value.is_active.return_value = False
        self.assertIsNone(cam.open_session(client))
        self.assertIsNone(cam.open_session(None))

    def test_open_session_closes_the_channel_when_sftp_fails(self):
        client = mock.Mock()
        client.get_transport.return_value.is_active.return_value = True
        client.open_sftp.side_effect = OSError("no sftp")
        with self.assertRaises(OSError):
            cam.open_session(client)
        self.assertTrue(client.get_transport.return_value.open_session.return_value.close.called)


class BrickScriptTest(unittest.TestCase):

    def test_script_compiles(self):
        compile(cam.BRICK_SCRIPT, "brick_camera", "exec")
        self.assertNotIn("f'", cam.BRICK_SCRIPT)   # Python 3.5 on the brick: no f-strings
        self.assertNotIn('f"', cam.BRICK_SCRIPT)

    def test_script_saves_where_the_pc_fetches(self):
        self.assertIn('os.path.expanduser("~/%s")' % cam.REMOTE_DIR, cam.BRICK_SCRIPT)
        self.assertIn('"%s"' % cam.REMOTE_FILE, cam.BRICK_SCRIPT)
        self.assertIn("os.rename(tmp, path)", cam.BRICK_SCRIPT)   # atomic replace
        self.assertIn('send(b"N"', cam.BRICK_SCRIPT)
        self.assertGreater(cam.FRAME_TIMEOUT, cam.SNAP_SECONDS)
        self.assertGreater(cam.LINGER_SECONDS, cam.SNAP_SECONDS)

    def test_command_carries_the_script_and_args(self):
        cmd = cam.brick_command(160, 120, 3, "/dev/video0", 5)
        encoded = base64.b64encode(cam.BRICK_SCRIPT.encode()).decode()
        self.assertIn(encoded, cmd)
        self.assertTrue(cmd.startswith("nice -n 19 python3 -u -c "))
        self.assertTrue(cmd.endswith(" 160 120 3 /dev/video0 5"))
        self.assertNotIn("'", cmd)   # nothing the brick's shell would trip over
        self.assertIn("base64 -d", cmd)
        self.assertTrue(cam.brick_command().endswith(
            f" {cam.WIDTH} {cam.HEIGHT} {cam.SNAP_SECONDS} {cam.DEVICE} {cam.FPS}"))
        self.assertIn("int(sys.argv[5])", cam.BRICK_SCRIPT)   # the script reads the frame rate

    def test_frame_rate_is_set_before_the_buffers(self):
        # The EV3's USB 1.1 port can't carry a webcam's default 30 fps: uvcvideo then never
        # completes a frame. The rate must be negotiated (VIDIOC_S_PARM) before REQBUFS.
        script = cam.BRICK_SCRIPT
        self.assertLess(script.index("S_PARM, parm"), script.index("REQBUFS, req"))
        self.assertIn('struct.pack_into("=II", parm, 12, 1, FPS)', script)   # timeperframe = 1/FPS
        self.assertLessEqual(cam.FPS, 10)   # what the Sonix cam on the brick's port streams at
        self.assertIn('W, H = struct.unpack_from("=II", fmt, 4)', script)   # takes the driver's size

    def test_v4l2_numbers(self):
        # ioctl codes for 32-bit ARM: dir<<30 | size<<16 | 'V'<<8 | nr
        def iowr(nr, size):
            return 0xC0000000 | size << 16 | ord("V") << 8 | nr
        self.assertIn("0x%08X" % iowr(5, 204), cam.BRICK_SCRIPT)    # VIDIOC_S_FMT
        self.assertIn("0x%08X" % iowr(22, 204), cam.BRICK_SCRIPT)   # VIDIOC_S_PARM
        self.assertIn("0x%08X" % iowr(8, 20), cam.BRICK_SCRIPT)     # VIDIOC_REQBUFS
        self.assertIn("0x%08X" % iowr(9, 68), cam.BRICK_SCRIPT)     # VIDIOC_QUERYBUF
        self.assertIn("0x%08X" % iowr(15, 68), cam.BRICK_SCRIPT)    # VIDIOC_QBUF
        self.assertIn("0x%08X" % iowr(17, 68), cam.BRICK_SCRIPT)    # VIDIOC_DQBUF
        self.assertIn("0x%08X" % (0x40000000 | 4 << 16 | ord("V") << 8 | 18), cam.BRICK_SCRIPT)
        self.assertEqual(struct.pack("<I", 0x47504A4D), b"MJPG")


if __name__ == "__main__":
    unittest.main()
