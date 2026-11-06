"""Simulated OldPhone CCTV camera for development - no Android phone needed.

Speaks the same protocol as the app: LAN beacon, key pairing, H.264 video (FFmpeg test pattern),
16 kHz audio (a quiet tone), status, and a motion event with a JPEG snapshot every --motion-every s.

    python dev/fake_phone.py --name "Garage" --port 47901
Run several with different --port/--name to test multi-camera. Needs ffmpeg on PATH or in tools/.
"""
import argparse
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
from media import find_tool, split_nals  # noqa: E402

T_HELLO, T_VIDEO, T_AUDIO, T_STATUS, T_MOTION, T_SNAPSHOT = 1, 2, 3, 4, 5, 6
T_TALK, T_CMD = 0x10, 0x11


def make_media(ffmpeg, name, tmp):
    h264, jpg = os.path.join(tmp, "v.h264"), os.path.join(tmp, "s.jpg")
    label = name.replace(":", " ").replace("'", "")
    vf = f"drawtext=text='{label}':fontsize=64:fontcolor=white:box=1:boxcolor=black@0.5:x=40:y=40"
    src = "testsrc2=size=1280x720:rate=15"
    for f in (vf, None):  # drawtext needs a font; fall back to the plain pattern
        args = [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", src, "-t", "20"]
        if f:
            args += ["-vf", f]
        args += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-profile:v", "high", "-g", "30",
                 "-x264-params", "aud=1", "-bsf:v", "h264_mp4toannexb", "-f", "h264", h264]
        if subprocess.run(args).returncode == 0:
            break
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180",
                    "-frames:v", "1", jpg], check=True)
    aus, cur = [], []
    for n in split_nals(open(h264, "rb").read()):
        if n[0] & 0x1F == 9 and cur:
            aus.append(cur); cur = []
        cur.append(n)
    aus.append(cur)
    sc = b"\x00\x00\x00\x01"
    config, frames = None, []
    for au in aus:
        ps = [n for n in au if n[0] & 0x1F in (7, 8)]
        if ps and config is None:
            config = b"".join(sc + n for n in ps)
        key = any(n[0] & 0x1F == 5 for n in au)
        frames.append((key, b"".join(sc + n for n in au if n[0] & 0x1F not in (7, 8, 9))))
    return config, frames, open(jpg, "rb").read()


class Phone:
    def __init__(self, a, config, frames, jpeg):
        self.a, self.config, self.frames, self.jpeg = a, config, frames, jpeg
        self.key = None
        self.cfg = {"camera": "0", "width": 1280, "height": 720, "fps": 15, "bitrate": 1000000, "audio": True,
                    "torch": False, "volume": 90, "motion": True, "sensitivity": 50}
        self.name = a.name
        self.talk_bytes = 0

    def pkt(self, t, payload):
        return bytes([t]) + struct.pack(">I", len(payload)) + payload

    def hello(self):
        return json.dumps({"id": self.a.id, "name": self.name, "model": "Simulated phone", "android": "dev",
                           "app": 2, "motionAvailable": True, "cfg": self.cfg, "video": "1280x720 @[15, 15]",
                           "audioRate": 16000, "cameras": [{"id": "0", "facing": "back", "mp": 12},
                                                            {"id": "1", "facing": "front", "mp": 8}]}).encode()

    def beacon(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while True:
            msg = json.dumps({"oldphonecctv": 1, "id": self.a.id, "name": self.name, "port": self.a.port}).encode()
            s.sendto(msg, ("127.0.0.1", 47800))
            time.sleep(3)

    def serve(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.a.port))
        srv.listen(2)
        print(f"[{self.name}] listening on {self.a.port}", flush=True)
        while True:
            c, addr = srv.accept()
            threading.Thread(target=self.session, args=(c,), daemon=True).start()

    def session(self, c):
        f = c.makefile("rb")
        lock = threading.Lock()

        def send(t, p):
            with lock:
                c.sendall(self.pkt(t, p))
        try:
            t, n = f.read(1)[0], struct.unpack(">I", f.read(4))[0]
            j = json.loads(f.read(n))
            if self.key is None:
                self.key = j.get("key")
            if j.get("key") != self.key:
                send(T_STATUS, b'{"error":"bad key"}')
                return
            send(T_HELLO, self.hello())
            stop = threading.Event()
            threading.Thread(target=self.stream, args=(send, stop), daemon=True).start()
            while True:
                hdr = f.read(5)
                if len(hdr) < 5:
                    break
                t, n = hdr[0], struct.unpack(">I", hdr[1:])[0]
                p = f.read(n)
                if t == T_TALK:
                    self.talk_bytes += len(p)
                elif t == T_CMD:
                    j = json.loads(p)
                    if j.get("cmd") == "set":
                        self.name = j.get("name", self.name)
                        self.cfg.update({k: v for k, v in j.items() if k in self.cfg})
                        send(T_HELLO, self.hello())
        except (OSError, IndexError, ValueError):
            pass
        finally:
            try:
                stop.set()
            except NameError:
                pass
            c.close()

    def stream(self, send, stop):
        send(T_VIDEO, bytes([2]) + struct.pack(">q", 0) + self.config)
        t0 = time.monotonic()
        base_us = int(time.monotonic() * 1e6)
        i, aud = 0, 0
        last_status = last_motion = 0
        motion_on = False
        while not stop.is_set():
            now = time.monotonic()
            key, fr = self.frames[i % len(self.frames)]
            send(T_VIDEO, bytes([1 if key else 0]) + struct.pack(">q", base_us + int(i * 1e6 / 15)) + fr)
            while aud * 0.04 <= (i + 1) / 15:
                pcm = b"".join(struct.pack("<h", int(600 * math.sin(2 * math.pi * 440 * (aud * 640 + k) / 16000)))
                               for k in range(640))
                send(T_AUDIO, struct.pack(">q", base_us + int(aud * 40000)) + pcm)
                aud += 1
            if now - last_status > 2:
                last_status = now
                send(T_STATUS, json.dumps({"battery": 80, "charging": True, "temp": 31.0, "fps": 15, "kbps": 900,
                                           "cam": "streaming", "video": "1280x720 @[15, 15]", "audio": True,
                                           "motion": motion_on, "error": None, "rssi": -50,
                                           "uptime": int(now - t0)}).encode())
            if self.a.motion_every and now - last_motion > self.a.motion_every and self.cfg["motion"]:
                last_motion = now
                motion_on = True
                send(T_MOTION, json.dumps({"on": True, "score": 0.08, "t": int(time.time() * 1000)}).encode())
                send(T_SNAPSHOT, self.jpeg)
                print(f"[{self.name}] motion event sent", flush=True)
            if motion_on and now - last_motion > 6:
                motion_on = False
                send(T_MOTION, json.dumps({"on": False, "peak": 0.12, "t": int(time.time() * 1000)}).encode())
            i += 1
            time.sleep(max(0, t0 + i / 15 - time.monotonic()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="Test camera")
    ap.add_argument("--id", default=None)
    ap.add_argument("--port", type=int, default=47901)
    ap.add_argument("--motion-every", type=int, default=30)
    a = ap.parse_args()
    a.id = a.id or "sim" + str(a.port)
    ffmpeg = find_tool("ffmpeg", "", ROOT)
    if not ffmpeg:
        sys.exit("ffmpeg not found")
    tmp = tempfile.mkdtemp(prefix="fakephone")
    try:
        config, frames, jpeg = make_media(ffmpeg, a.name, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    p = Phone(a, config, frames, jpeg)
    threading.Thread(target=p.beacon, daemon=True).start()
    p.serve()


if __name__ == "__main__":
    main()
