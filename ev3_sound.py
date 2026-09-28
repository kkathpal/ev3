"""Sound on the EV3's speaker, shared by the apps: built-in effects, uploads, speech, volume.

The command strings are for a long-lived shell on the brick. Anything that plays runs in
the background (`( … ) &`), because the same shell also carries motor commands and they
must never wait behind a sound.
"""
import os
import re
import shlex

SOUND_DIR = "/usr/share/sounds/ev3dev"   # the brick's built-in effects, one folder per group
MY_SOUNDS = "sounds"                     # uploads go here, in the robot user's home folder
MY_GROUP = "my sounds"
HORN = SOUND_DIR + "/mechanical/horn_1.wav"
VOLUME_STEP = 10                         # % per − / + click

# Shell lines printing "vol=<percent>", "sounds=<path> <path> …" and "home=<dir>"; parse() reads them.
QUERY = (
    "echo \"vol=$(amixer sget PCM | sed -n 's/.*\\[\\([0-9]*\\)%\\].*/\\1/p' | head -n 1)\"\n"
    f"echo \"sounds=$(ls {SOUND_DIR}/*/*.wav $HOME/{MY_SOUNDS}/*.wav 2>/dev/null | tr '\\n' ' ')\"\n"
    "echo \"home=$HOME\"\n"
)

STOP = "pkill -x espeak; pkill -x aplay"


def play(path):
    return f"({STOP}; aplay -q {shlex.quote(path)}) &"


def say(text):
    return f"({STOP}; espeak -a 200 -s 150 --stdout {shlex.quote(text)} | aplay -q) &"


def say_and_keep(text, home):
    """Speak `text` and keep it as a WAV in ~/sounds, so it joins "my sounds" for replaying.

    Returns (command, brick path). The same text reuses the same file.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40].rstrip("_") or "speech"
    path = f"{home}/{MY_SOUNDS}/said_{slug}.wav"
    q = shlex.quote
    cmd = (f"({STOP}; mkdir -p {q(home + '/' + MY_SOUNDS)} && "
           f"espeak -a 200 -s 150 -w {q(path)} {q(text)} && aplay -q {q(path)}) &")
    return cmd, path


def horn():
    """Honk, unless a sound is already playing (a held key repeats)."""
    return f"(pgrep -x aplay >/dev/null || aplay -q {HORN}) &"


def set_volume(percent):
    return f"amixer -q sset PCM {max(0, min(100, int(percent)))}%"


def parse(text):
    """QUERY output -> (volume % or None, [sound paths], home folder or None)."""
    volume, sounds, home = None, [], None
    for line in text.splitlines():
        key, _, val = line.partition("=")
        if key == "vol" and val.strip().isdigit():
            volume = int(val)
        elif key == "sounds":
            sounds = val.split()
        elif key == "home" and val.strip():
            home = val.strip()
    return volume, sounds, home


def group(path):
    """/usr/share/sounds/ev3dev/animals/dog_bark_1.wav -> animals; uploads -> my sounds."""
    return path.split("/")[-2] if path.startswith(SOUND_DIR + "/") else MY_GROUP


def name(path):
    """…/dog_bark_1.wav -> dog bark 1"""
    return os.path.splitext(path.rsplit("/", 1)[-1])[0].replace("_", " ")


def grouped(paths):
    """[(group, [paths])] with the user's own sounds first, then groups A–Z."""
    groups = {}
    for path in paths:
        groups.setdefault(group(path), []).append(path)
    return sorted(groups.items(), key=lambda g: (g[0] != MY_GROUP, g[0]))


def upload(client, local_path):
    """Copy a WAV to ~/sounds on the brick over SFTP (paramiko `client`).

    Returns (brick path or None, message). Slow over Bluetooth: call it off the UI thread.
    """
    stem, ext = os.path.splitext(os.path.basename(local_path))
    if ext.lower() != ".wav":
        return None, "The brick plays .wav files only: convert it to WAV first"
    filename = (re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "sound") + ".wav"
    try:
        sftp = client.open_sftp()
        try:
            try:
                sftp.mkdir(MY_SOUNDS)
            except OSError:
                pass   # already there
            sftp.put(local_path, f"{MY_SOUNDS}/{filename}")
            path = f"{sftp.normalize(MY_SOUNDS)}/{filename}"
        finally:
            sftp.close()
    except Exception as e:
        return None, f"Upload failed: {e}"
    return path, f"Uploaded {filename} · find it under {MY_GROUP}"
