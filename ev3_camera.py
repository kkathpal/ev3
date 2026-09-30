"""Camera snapshots from a USB webcam on the EV3 brick, for the phone page.

Nothing is installed on the brick: over the SSH connection the drive apps already use, this
runs a small pure-Python V4L2 capture script (BRICK_SCRIPT, Python 3.5 as on ev3dev). It asks
the webcam for MJPEG (most UVC webcams offer it), keeps the stream draining, and every
SNAP_SECONDS saves one JPEG to `/dev/shm/ev3-camera/latest.jpg` in the brick's RAM (written to a temp
file and renamed, so the PC never reads half a picture). Each save is announced on stdout as
`kind(1 byte) + length(4 bytes, big-endian) + data`: kind "N" (data: the file size as digits),
or "E" with an error message. `Camera` reads those on the PC, fetches the file over SFTP and
hands the newest picture to every phone asking (`latest()`).

Safety/load: the capture only runs while a phone is watching (it stops LINGER_SECONDS after
the last viewer leaves), runs at `nice 19`, and ends by itself when the SSH session closes
(its next write fails) or the camera stalls. It never touches the motors.
"""
import base64
import contextlib
import socket
import struct
import threading
import time

WIDTH, HEIGHT = 160, 120   # asked for; the driver picks the nearest size the camera offers (see FPS)
FPS = 5                    # frames per second asked for: the brick's USB 1.1 port can't carry a
                           # webcam's default 30 fps, and then uvcvideo never completes a frame
SNAP_SECONDS = 3           # one snapshot this often
DEVICE = "/dev/video0"
REMOTE_DIR = "/dev/shm/ev3-camera"   # the brick's RAM (tmpfs), not the SD card; BRICK_SCRIPT has the same
REMOTE_FILE = "latest.jpg"
LINGER_SECONDS = 8         # keep capturing this long after the last viewer leaves (phones poll every 3 s)
FRAME_TIMEOUT = 15         # no snapshot for this long (> SNAP_SECONDS): report the camera as stalled
MAX_FRAME = 2_000_000      # sanity limit on one JPEG

# Runs on the brick. ioctl numbers are the 32-bit ARM ones (struct sizes 204/68/20).
BRICK_SCRIPT = r'''
import fcntl, mmap, os, select, struct, sys, time
W, H, SNAP, DEV, FPS = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), sys.argv[4], int(sys.argv[5])
DIR, FILE, WARMUP = "/dev/shm/ev3-camera", "latest.jpg", 3
out = sys.stdout.buffer
def send(kind, data=b""):
    out.write(kind + struct.pack(">I", len(data)) + data)
    out.flush()
def fail(text):
    send(b"E", text.encode())
    sys.exit(1)
S_FMT, S_PARM, REQBUFS, QUERYBUF = 0xC0CC5605, 0xC0CC5616, 0xC0145608, 0xC0445609
QBUF, DQBUF, STREAMON, STREAMOFF = 0xC044560F, 0xC0445611, 0x40045612, 0x40045613
MJPG = 0x47504A4D
try:
    fd = os.open(DEV, os.O_RDWR)
except OSError as e:
    fail("Can't open %s: %s" % (DEV, e.strerror))
fmt = bytearray(struct.pack("=IIIII", 1, W, H, MJPG, 0).ljust(204, b"\0"))
try:
    fcntl.ioctl(fd, S_FMT, fmt, True)
except OSError as e:
    fail("Camera refused %dx%d MJPEG: %s" % (W, H, e.strerror))
if struct.unpack_from("=I", fmt, 12)[0] != MJPG:
    fail("This camera doesn't offer MJPEG.")
W, H = struct.unpack_from("=II", fmt, 4)   # the size the camera offers nearest to the one asked for
try:   # every other failure is sent to the PC too, not just to a stderr nobody reads
    # Frame rate, before the buffers: without it uvcvideo streams at the camera's default (30
    # fps), more than the EV3's full-speed USB carries, and no frame ever completes. A driver
    # that can't set it just keeps its default.
    parm = bytearray(204)
    struct.pack_into("=I", parm, 0, 1)
    struct.pack_into("=II", parm, 12, 1, FPS)   # capture.timeperframe = 1/FPS s
    try:
        fcntl.ioctl(fd, S_PARM, parm, True)
    except OSError:
        pass
    os.makedirs(DIR, exist_ok=True)
    path, tmp = os.path.join(DIR, FILE), os.path.join(DIR, FILE + ".tmp")
    try:
        os.remove(path)   # no picture from an earlier run is left in RAM to be mistaken for a new one
    except OSError:
        pass
    req = bytearray(struct.pack("=III", 4, 1, 1).ljust(20, b"\0"))
    fcntl.ioctl(fd, REQBUFS, req, True)
    count = struct.unpack_from("=I", req, 0)[0]
    if count == 0:
        fail("The camera gave no capture buffers.")
    maps = []
    for i in range(count):
        b = bytearray(68)
        struct.pack_into("=II", b, 0, i, 1)
        struct.pack_into("=I", b, 48, 1)
        fcntl.ioctl(fd, QUERYBUF, b, True)
        length, offset = struct.unpack_from("=I", b, 56)[0], struct.unpack_from("=I", b, 52)[0]
        maps.append(mmap.mmap(fd, length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=offset))
        fcntl.ioctl(fd, QBUF, b, True)
    fcntl.ioctl(fd, STREAMON, struct.pack("=i", 1))
    skipped, due = 0, None   # webcams give dark first frames; then the first snapshot at once
    while True:   # frames are drained continuously so the driver's buffer queue never stalls
        if not select.select([fd], [], [], 5)[0]:
            fail("The camera stopped sending frames.")
        b = bytearray(68)
        struct.pack_into("=I", b, 4, 1)
        struct.pack_into("=I", b, 48, 1)
        fcntl.ioctl(fd, DQBUF, b, True)
        index, used = struct.unpack_from("=I", b, 0)[0], struct.unpack_from("=I", b, 8)[0]
        frame = maps[index][:used]
        struct.pack_into("=II", b, 8, 0, 0)   # bytesused and flags are the driver's to fill
        fcntl.ioctl(fd, QBUF, b, True)
        if skipped < WARMUP:
            skipped += 1
            continue
        now = time.time()
        if (due is not None and now < due) or frame[:2] != b"\xff\xd8":
            continue
        with open(tmp, "wb") as f:
            f.write(frame)
        os.rename(tmp, path)   # atomic: the PC sees the old picture or the new one, never a part
        due = now + SNAP
        send(b"N", str(len(frame)).encode())
except BrokenPipeError:
    raise SystemExit(0)   # the PC closed the stream: done
except (OSError, ValueError, IndexError) as e:
    fail("Camera error: %s" % e)
'''


def brick_command(width=WIDTH, height=HEIGHT, snap=SNAP_SECONDS, device=DEVICE, fps=FPS):
    """Shell command for the brick: the script (base64, so quoting can't break it) at lowest priority."""
    script = base64.b64encode(BRICK_SCRIPT.encode()).decode()
    return f'nice -n 19 python3 -u -c "$(echo {script} | base64 -d)" {width} {height} {snap} {device} {fps}'


def read_message(read):
    """One (kind, data) from `read(n)` (which returns exactly n bytes, or raises EOFError)."""
    head = read(5)
    kind, size = head[:1], struct.unpack(">I", head[1:])[0]
    if size > MAX_FRAME:
        raise ValueError("camera message too large")
    return kind, read(size) if size else b""


class Camera:
    """Newest snapshot from the brick's webcam, shared by every phone watching.

    `opener()` returns a session already running `brick_command()`: channel-like (`recv`,
    `settimeout`, `close`) plus `fetch()` returning the saved file's bytes; or None when the
    brick isn't connected."""

    def __init__(self, opener):
        self.opener = opener
        self.cond = threading.Condition()
        self.frame, self.seq, self.taken = None, 0, None
        self.viewers = 0
        self.running = False
        self.status = "Off"

    def state(self):
        with self.cond:
            live = self.running and self.frame is not None
            return {"status": self.status, "live": live, "interval": SNAP_SECONDS,
                    "age": round(time.time() - self.taken, 1) if live else None}

    @contextlib.contextmanager
    def viewing(self):
        """Hold this while a phone watches; the first viewer starts the capture."""
        with self.cond:
            self.viewers += 1
            start = not self.running
            if start:
                self.running, self.frame, self.status = True, None, "Starting…"
        if start:
            threading.Thread(target=self._run, daemon=True).start()
        try:
            yield
        finally:
            with self.cond:
                self.viewers -= 1

    def latest(self, timeout=10):
        """The newest JPEG, waiting up to `timeout` for the session's first one; None if none came
        (timed out, or the capture ended without a picture)."""
        with self.cond:
            self.cond.wait_for(lambda: self.frame is not None or not self.running, timeout=timeout)
            return self.frame

    def _run(self):
        while self._capture():
            pass

    def _capture(self):
        """One capture session; True when it should start over (a viewer arrived as it went idle)."""
        session = None
        try:
            session = self.opener()
            if session is None:
                return self._finish("Brick not connected")
            session.settimeout(1.0)
            # `quiet_since` and `empty_since` are cells shared with read(): it always sees the
            # latest values, and updates `empty_since` itself so a viewer leaving during a long
            # wait (camera stalled) still ends the capture after LINGER_SECONDS.
            quiet_since, empty_since = time.monotonic(), None

            def read(n):
                nonlocal empty_since
                data = b""
                while len(data) < n:
                    with self.cond:
                        empty_since = None if self.viewers else (empty_since or time.monotonic())
                    if empty_since is not None and time.monotonic() - empty_since > LINGER_SECONDS:
                        raise EOFError("idle")
                    try:
                        chunk = session.recv(n - len(data))
                    except socket.timeout:
                        if time.monotonic() - quiet_since > FRAME_TIMEOUT:
                            raise EOFError("The camera stopped sending frames.")
                        continue
                    if not chunk:
                        raise EOFError("The brick closed the camera stream.")
                    data += chunk
                return data

            while True:
                kind, data = read_message(read)
                quiet_since = time.monotonic()
                if kind == b"E":
                    return self._finish(data.decode(errors="replace"))
                if kind != b"N":
                    continue
                # A bad fetch is shown but doesn't end the capture: the next snapshot may be fine.
                try:
                    jpeg = session.fetch()
                except Exception as e:
                    self._note(f"Couldn't fetch the snapshot: {e}")
                    continue
                if not jpeg or jpeg[:2] != b"\xff\xd8" or len(jpeg) > MAX_FRAME:
                    self._note("The snapshot on the brick isn't a JPEG.")
                    continue
                with self.cond:
                    self.frame, self.seq, self.taken = jpeg, self.seq + 1, time.time()
                    self.status = "Live"
                    self.cond.notify_all()
        except EOFError as e:
            return self._finish("Off" if str(e) == "idle" else str(e))
        except Exception as e:
            return self._finish(f"Camera error: {e}")
        finally:
            if session is not None:
                try:
                    session.close()   # the brick script's next write fails and it exits
                except Exception:
                    pass

    def _note(self, status):
        with self.cond:
            self.status = status
            self.cond.notify_all()

    def _finish(self, status):
        """End the capture. A viewer who arrived just as it went idle would otherwise find it
        stopped and no one to restart it, so in that case it stays `running` and starts over."""
        with self.cond:
            restart = status == "Off" and self.viewers > 0
            if restart:
                self.frame, self.status = None, "Starting…"
            else:
                self.running, self.status = False, status
            self.cond.notify_all()
            return restart


class Session:
    """The brick script's channel plus an SFTP client to fetch the snapshot it announces."""

    def __init__(self, channel, sftp):
        self.channel, self.sftp = channel, sftp

    def settimeout(self, seconds):
        self.channel.settimeout(seconds)

    def recv(self, n):
        return self.channel.recv(n)

    def fetch(self):
        with self.sftp.open(REMOTE_DIR + "/" + REMOTE_FILE, "rb") as f:
            return f.read()

    def close(self):
        for part in (self.channel, self.sftp):
            try:
                part.close()
            except Exception:
                pass


def open_session(client):
    """Start the capture on the brick over `client`'s SSH connection (its own channel and SFTP
    session, separate from the drive shells, so it can't delay driving). None if there's no
    connection."""
    transport = client.get_transport() if client is not None else None
    if transport is None or not transport.is_active():
        return None
    channel = transport.open_session()
    try:
        channel.exec_command(brick_command())
        sftp = client.open_sftp()
    except Exception:
        channel.close()
        raise
    return Session(channel, sftp)


open_channel = open_session   # older name
