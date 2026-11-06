"""OldPhone CCTV PC server: phone link, live viewer (HTTP + WebSocket), recorder, retention, phone modes.

Standard library only (Python 3.9+). Run:  python server/cctv.py
Other commands:
    python server/cctv.py install-phone          install/refresh the camera app on the USB/adb phone
    python server/cctv.py mode surveillance      strip the phone down (reversible)
    python server/cctv.py mode normal            put the phone back exactly as it was
    python server/cctv.py mode surveillance --dry-run   only list what would change
"""
import asyncio
import base64
import datetime as dt
import glob
import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
CONFIG_PATH = os.path.join(ROOT, "config.json")
PHONE_PKG = "com.oldphonecctv.camera"
PHONE_PORT = 47801
BEACON_PORT = 47800

DEFAULTS = {
    "name": "Old Phone CCTV",
    "http_port": 8090,
    "bind": "127.0.0.1",          # tailscale serve / localhost; set 0.0.0.0 to open on the LAN
    "phone_host": "auto",          # auto = find it on Wi-Fi (beacon) or over USB; or an IP / Tailscale IP
    "phone_key": "",               # generated on first run, pushed to the phone by install-phone
    "recordings_dir": "recordings",
    "record": True,
    "retention_hours": 48,
    "segment_minutes": 5,
    "audio_bitrate": "32k",
    "rotate": 0,                   # degrees clockwise applied to live view and recordings
    "min_free_gb": 5,
    "password": "",                # optional HTTP Basic password (user name: anything)
    "ffmpeg": "",
    "adb": "",
}

FRAG_SUFFIX = ".rec.mp4"   # clip still being written (or left over from a crash)
T_HELLO, T_VIDEO, T_AUDIO, T_STATUS = 1, 2, 3, 4
T_TALK, T_CMD = 0x10, 0x11


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- config / tools

def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:  # tolerate a BOM from Notepad/PowerShell
            cfg.update(json.load(f))
    changed = False
    if not cfg.get("phone_key"):
        cfg["phone_key"] = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(10))
        changed = True
    if changed or not os.path.exists(CONFIG_PATH):
        save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def find_tool(name, configured):
    exe = name + (".exe" if os.name == "nt" else "")
    cands = []
    if configured:
        cands.append(configured)
    cands += [os.path.join(ROOT, "tools", exe), os.path.join(ROOT, "tools", "platform-tools", exe)]
    found = shutil.which(name)
    if found:
        cands.append(found)
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA", "")
        cands += glob.glob(os.path.join(la, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*", "bin", exe))
        cands += [os.path.join(la, "Android", "Sdk", "platform-tools", exe)]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def recordings_dir(cfg):
    d = cfg["recordings_dir"]
    if not os.path.isabs(d):
        d = os.path.join(ROOT, d)
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------- H.264 helpers

def split_nals(annexb):
    """Split an Annex-B byte string into NAL units (without start codes)."""
    out, i, n = [], 0, len(annexb)
    starts = []
    while i < n - 3:
        if annexb[i] == 0 and annexb[i + 1] == 0:
            if annexb[i + 2] == 1:
                starts.append((i, i + 3)); i += 3; continue
            if annexb[i + 2] == 0 and i + 3 < n and annexb[i + 3] == 1:
                starts.append((i, i + 4)); i += 4; continue
        i += 1
    for k, (s, e) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else n
        if end > e:
            out.append(annexb[e:end])
    if not starts and annexb:
        out.append(annexb)
    return out


def avcc_from_config(config):
    nals = split_nals(config)
    sps = next((x for x in nals if x[0] & 0x1F == 7), None)
    pps = next((x for x in nals if x[0] & 0x1F == 8), None)
    if not sps or not pps:
        return None
    return (bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1]) + struct.pack(">H", len(sps)) + sps
            + bytes([1]) + struct.pack(">H", len(pps)) + pps)


def annexb_to_avcc(frame):
    return b"".join(struct.pack(">I", len(n)) + n for n in split_nals(frame) if n[0] & 0x1F not in (7, 8, 9))


# --------------------------------------------------------------------------- Matroska (live, unknown sizes)

def _vint_size(n):
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | n).to_bytes(length, "big")
    raise ValueError("too big")


def _id(i):
    return i.to_bytes((i.bit_length() + 7) // 8, "big")


def el(eid, data):
    return _id(eid) + _vint_size(len(data)) + data


def uint(eid, v):
    return el(eid, v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big"))


def mkv_header(avcc, width, height, audio_rate):
    ebml = el(0x1A45DFA3, uint(0x4286, 1) + uint(0x42F7, 1) + uint(0x42F2, 4) + uint(0x42F3, 8)
              + el(0x4282, b"matroska") + uint(0x4287, 4) + uint(0x4285, 2))
    info = el(0x1549A966, uint(0x2AD7B1, 1_000_000) + el(0x4D80, b"oldphonecctv") + el(0x5741, b"oldphonecctv"))
    video = el(0xAE, uint(0xD7, 1) + uint(0x73C5, 1) + uint(0x83, 1) + uint(0x9C, 0)
               + el(0x86, b"V_MPEG4/ISO/AVC") + el(0x63A2, avcc)
               + el(0xE0, uint(0xB0, width) + uint(0xBA, height)))
    tracks = video
    if audio_rate:
        tracks += el(0xAE, uint(0xD7, 2) + uint(0x73C5, 2) + uint(0x83, 2) + uint(0x9C, 0)
                     + el(0x86, b"A_PCM/INT/LIT")
                     + el(0xE1, el(0xB5, struct.pack(">d", float(audio_rate))) + uint(0x9F, 1) + uint(0x6264, 16)))
    segment = _id(0x18538067) + b"\x01\xFF\xFF\xFF\xFF\xFF\xFF\xFF"
    return ebml + segment + info + el(0x1654AE6B, tracks)


def mkv_cluster(ts_ms):
    return _id(0x1F43B675) + b"\x01\xFF\xFF\xFF\xFF\xFF\xFF\xFF" + uint(0xE7, ts_ms)


def mkv_block(track, rel_ms, key, data):
    return el(0xA3, bytes([0x80 | track]) + struct.pack(">h", rel_ms) + bytes([0x80 if key else 0]) + data)


# --------------------------------------------------------------------------- recorder

class Recorder:
    """Feeds the phone's stream to ffmpeg as live Matroska; ffmpeg cuts clock-aligned MP4 clips."""

    def __init__(self, app):
        self.app = app
        self.proc = None
        self.base_us = None
        self.cluster_ms = None
        self.has_audio = False
        self.started_at = None
        self.pending = 0
        self.error = None
        self.stderr_task = None

    @property
    def active(self):
        return self.proc is not None and self.proc.returncode is None

    async def start(self, config, has_audio, width, height):
        cfg = self.app.cfg
        ffmpeg = find_tool("ffmpeg", cfg.get("ffmpeg"))
        if not ffmpeg:
            self.error = "FFmpeg not found - run setup.ps1 or put ffmpeg.exe in tools\\"
            return
        avcc = avcc_from_config(config)
        if not avcc:
            return
        # Fragmented while being written so a crash or power cut never loses a clip;
        # App.finalize_loop repacks each closed clip into a normal, fast-seeking MP4.
        out = os.path.join(recordings_dir(cfg), "%Y-%m-%d_%H-%M-%S" + FRAG_SUFFIX)
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        rot = int(cfg.get("rotate", 0)) % 360
        if rot:
            args += ["-display_rotation:v:0", str((360 - rot) % 360)]
        args += ["-f", "matroska", "-i", "pipe:0", "-map", "0:v"]
        if has_audio:
            args += ["-map", "0:a", "-c:a", "aac", "-b:a", cfg["audio_bitrate"], "-af", "aresample=async=1000"]
        args += ["-c:v", "copy", "-f", "segment", "-segment_time", str(int(cfg["segment_minutes"]) * 60),
                 "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
                 "-segment_format", "mp4", "-segment_format_options",
                 "movflags=+frag_keyframe+empty_moov+default_base_moof", out]
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        self.proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.PIPE, **kw)
        self.stderr_task = asyncio.create_task(self._read_stderr(self.proc))
        self.has_audio = has_audio
        self.base_us = None
        self.cluster_ms = None
        self.started_at = time.time()
        self.error = None
        self._write(mkv_header(avcc, width, height, 16000 if has_audio else 0))
        log(f"recording started ({width}x{height}{', audio' if has_audio else ''}, rotate {rot})")

    async def _read_stderr(self, proc):
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            msg = line.decode(errors="replace").strip()
            if msg:
                self.error = msg
                log("ffmpeg:", msg)

    def _write(self, data):
        if not self.active:
            return
        try:
            self.proc.stdin.write(data)
            self.pending += len(data)
        except (BrokenPipeError, ConnectionResetError):
            self.error = "ffmpeg stopped unexpectedly"

    async def drain(self):
        if self.active and self.pending > 256_000:
            self.pending = 0
            try:
                await asyncio.wait_for(self.proc.stdin.drain(), 5)
            except Exception:
                pass

    def video(self, pts_us, key, frame):
        if not self.active:
            return
        if self.base_us is None:
            if not key:
                return
            self.base_us = pts_us
        ms = (pts_us - self.base_us) // 1000
        if ms < 0:
            return
        if key or self.cluster_ms is None or ms - self.cluster_ms > 30_000:
            self.cluster_ms = ms
            self._write(mkv_cluster(ms))
        data = annexb_to_avcc(frame)
        if data:
            self._write(mkv_block(1, ms - self.cluster_ms, key, data))

    def audio(self, pts_us, pcm):
        if not self.active or not self.has_audio or self.base_us is None or self.cluster_ms is None:
            return
        rel = (pts_us - self.base_us) // 1000 - self.cluster_ms
        if -32768 <= rel <= 32767 and pts_us >= self.base_us:
            self._write(mkv_block(2, rel, True, pcm))

    async def stop(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 15)
        except Exception:
            proc.kill()
        log("recording stopped")


# --------------------------------------------------------------------------- viewers (WebSocket)

class WebSocket:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.closed = False

    async def send(self, data, binary=True):
        if self.closed:
            return
        op = 0x2 if binary else 0x1
        if not binary:
            data = data.encode()
        n = len(data)
        if n < 126:
            hdr = bytes([0x80 | op, n])
        elif n < 65536:
            hdr = bytes([0x80 | op, 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([0x80 | op, 127]) + struct.pack(">Q", n)
        self.w.write(hdr + data)
        await self.w.drain()

    async def recv(self):
        """Returns (opcode, payload) for text/binary messages; None when closed."""
        buf, first_op = b"", None
        while True:
            h = await self.r.readexactly(2)
            fin, op = h[0] & 0x80, h[0] & 0x0F
            n = h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", await self.r.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", await self.r.readexactly(8))[0]
            if n > 4_000_000:
                return None
            mask = await self.r.readexactly(4) if h[1] & 0x80 else b"\0\0\0\0"
            payload = bytearray(await self.r.readexactly(n))
            for i in range(n):
                payload[i] ^= mask[i & 3]
            if op == 0x8:
                return None
            if op == 0x9:
                self.w.write(bytes([0x8A, len(payload)]) + bytes(payload[:125]))
                continue
            if op == 0xA:
                continue
            if op != 0:
                first_op = op
            buf += payload
            if fin:
                return first_op, bytes(buf)


class StreamSink:
    """Writes viewer messages as chunks of a long-lived HTTP response."""

    def __init__(self, writer):
        self.w = writer
        self.closed = False

    async def send(self, data, binary=True):
        if self.closed:
            return
        if not binary:
            data = b"\x00" + data.encode()  # type 0 = JSON text
        msg = struct.pack(">I", len(data)) + data
        try:
            self.w.write(b"%x\r\n" % len(msg) + msg + b"\r\n")
            await self.w.drain()
        except Exception:
            self.closed = True
            raise


class Viewer:
    MAX_QUEUED = 4_000_000

    def __init__(self, ws, addr):
        self.ws = ws
        self.addr = addr
        self.q = asyncio.Queue()
        self.queued = 0
        self.need_key = True
        self.listen = False

    def push(self, item, size, droppable=False):
        if droppable and self.queued > self.MAX_QUEUED:
            self.need_key = True
            return
        self.queued += size
        self.q.put_nowait((item, size))

    async def pump(self):
        while True:
            item, size = await self.q.get()
            self.queued -= size
            if item is None:
                return
            if isinstance(item, str):
                await self.ws.send(item, binary=False)
            else:
                await self.ws.send(item)


# --------------------------------------------------------------------------- the app

class App:
    def __init__(self):
        self.cfg = load_config()
        self.viewers = set()
        self.stream_viewers = {}
        self.recorder = Recorder(self)
        self.phone_writer = None
        self.phone_addr = None
        self.phone_connected_at = None
        self.discovered = None
        self.hello = None
        self.status = None
        self.config_annexb = None
        self.last_key_request = 0
        self.talking = None
        self.phone_error = None
        self.rec_restart = False
        self.mode_busy = False

    # ---------------------------------------------------------------- phone link

    async def phone_loop(self):
        while True:
            host, port = await self.pick_phone_target()
            if not host:
                self.phone_error = "Looking for the phone on Wi-Fi and USB..."
                await asyncio.sleep(3)
                continue
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
            except Exception as e:
                self.phone_error = f"Can't reach the phone at {host}:{port} ({e.__class__.__name__})"
                await asyncio.sleep(3)
                continue
            self.phone_writer, self.phone_addr = w, f"{host}:{port}"
            self.phone_connected_at = time.time()
            self.send_cmd({"cmd": "auth", "key": self.cfg["phone_key"]})
            pinger = asyncio.create_task(self.ping_phone())
            try:
                await self.read_phone(r)
            except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, OSError):
                pass
            except Exception as e:
                log("phone link error:", repr(e))
            finally:
                pinger.cancel()
                try:
                    w.close()
                except Exception:
                    pass
                had_hello = self.hello is not None
                self.phone_writer = None
                self.hello = self.status = self.config_annexb = None
                self.phone_connected_at = None
                await self.recorder.stop()
                if had_hello:
                    log("phone disconnected")
                    self.phone_error = "Lost the phone - reconnecting..."
                elif not self.phone_error or "rejected" not in self.phone_error:
                    self.phone_error = (f"Found {host} but the OldPhone CCTV app isn't answering. "
                                        "Open the OldPhone CCTV app on the phone and tap Start camera.")
                self.broadcast_state()
            await asyncio.sleep(3)

    async def pick_phone_target(self):
        h = self.cfg.get("phone_host", "auto")
        if h and h != "auto":
            return h, PHONE_PORT
        if self.discovered and time.time() - self.discovered[1] < 60:
            return self.discovered[0], PHONE_PORT
        # No beacon heard: try a USB-connected phone through an adb port forward.
        adb = find_tool("adb", self.cfg.get("adb"))
        if adb:
            res = await run_cmd([adb, "forward", f"tcp:{PHONE_PORT + 10}", f"tcp:{PHONE_PORT}"], timeout=8)
            if res[0] == 0:
                return "127.0.0.1", PHONE_PORT + 10
        return None, None

    async def ping_phone(self):
        while True:
            await asyncio.sleep(5)
            self.send_cmd({"cmd": "ping"})

    def send_phone(self, t, payload):
        w = self.phone_writer
        if w is None:
            return False
        w.write(bytes([t]) + struct.pack(">I", len(payload)) + payload)
        return True

    def send_cmd(self, obj):
        return self.send_phone(T_CMD, json.dumps(obj).encode())

    def request_key(self):
        now = time.time()
        if now - self.last_key_request > 1:
            self.last_key_request = now
            self.send_cmd({"cmd": "key"})

    async def read_phone(self, r):
        while True:
            hdr = await asyncio.wait_for(r.readexactly(5), 20)
            t, n = hdr[0], struct.unpack(">I", hdr[1:])[0]
            data = await asyncio.wait_for(r.readexactly(n), 20)
            if t == T_VIDEO:
                await self.on_video(data)
            elif t == T_AUDIO:
                self.on_audio(data)
            elif t == T_STATUS:
                self.status = json.loads(data)
                if self.status.get("error") == "bad key":
                    self.phone_error = "The phone rejected the pairing key - run install-phone again"
                self.broadcast_state()
            elif t == T_HELLO:
                old = self.hello
                self.hello = json.loads(data)
                self.phone_error = None
                if old is None:
                    log("phone connected", self.phone_addr, self.hello.get("model"), self.hello.get("video"))
                if old and (old.get("cfg", {}).get("audio") != self.hello.get("cfg", {}).get("audio")
                            or old.get("video") != self.hello.get("video")):
                    self.rec_restart = True
                self.broadcast_state()
            await self.recorder.drain()

    async def on_video(self, data):
        flags = data[0]
        pts = struct.unpack(">q", data[1:9])[0]
        frame = data[9:]
        if flags & 2:
            if self.config_annexb != frame:
                if self.config_annexb is not None:
                    self.rec_restart = True
                self.config_annexb = frame
            for v in self.viewers:
                v.push(bytes([1]) + frame, len(frame))
            return
        key = bool(flags & 1)
        if key and (self.rec_restart or (self.cfg["record"] and not self.recorder.active)):
            self.rec_restart = False
            await self.recorder.stop()
            if self.cfg["record"] and self.config_annexb:
                w, h = self.video_size()
                await self.recorder.start(self.config_annexb, bool(self.hello and self.hello.get("cfg", {}).get("audio")), w, h)
        self.recorder.video(pts, key, frame)
        msg = None
        for v in self.viewers:
            if v.need_key:
                if not key:
                    self.request_key()
                    continue
                v.need_key = False
            if msg is None:
                body = (self.config_annexb + frame) if key and self.config_annexb else frame
                msg = bytes([2, 1 if key else 0]) + struct.pack(">q", pts) + body
            v.push(msg, len(msg), droppable=not key)

    def on_audio(self, data):
        pts = struct.unpack(">q", data[:8])[0]
        pcm = data[8:]
        self.recorder.audio(pts, pcm)
        msg = None
        for v in self.viewers:
            if v.listen and v is not self.talking:
                if msg is None:
                    msg = bytes([3]) + pcm
                v.push(msg, len(msg), droppable=True)

    def video_size(self):
        try:
            wh = self.hello["video"].split(" ")[0].split("x")
            return int(wh[0]), int(wh[1])
        except Exception:
            return 1280, 720

    # ---------------------------------------------------------------- discovery

    def start_beacon_listener(self, loop):
        app = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    j = json.loads(data)
                    if j.get("oldphonecctv"):
                        if not app.discovered or app.discovered[0] != addr[0]:
                            log("found phone on Wi-Fi:", addr[0], j.get("name"))
                        app.discovered = (addr[0], time.time())
                except Exception:
                    pass

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", BEACON_PORT))
            return loop.create_datagram_endpoint(Proto, sock=sock)
        except OSError as e:
            log("beacon listener unavailable:", e)
            return None

    # ---------------------------------------------------------------- state for viewers

    def state(self):
        rec_dir = recordings_dir(self.cfg)
        try:
            du = shutil.disk_usage(rec_dir)
            free_gb = round(du.free / 1e9, 1)
        except Exception:
            free_gb = None
        used = sum(os.path.getsize(f) for f in glob.glob(os.path.join(rec_dir, "*.mp4")))
        return {
            "t": "state",
            "name": self.cfg["name"],
            "phone": {
                "connected": self.phone_writer is not None and self.hello is not None,
                "addr": self.phone_addr,
                "since": self.phone_connected_at,
                "hello": self.hello,
                "status": self.status,
                "error": self.phone_error,
            },
            "rec": {
                "enabled": self.cfg["record"],
                "active": self.recorder.active,
                "error": self.recorder.error,
                "used_gb": round(used / 1e9, 2),
                "free_gb": free_gb,
                "retention_hours": self.cfg["retention_hours"],
                "segment_minutes": self.cfg["segment_minutes"],
                "dir": rec_dir,
            },
            "rotate": self.cfg.get("rotate", 0),
            "viewers": len(self.viewers),
            "talking": self.talking is not None,
        }

    def broadcast_state(self):
        s = json.dumps(self.state())
        for v in self.viewers:
            v.push(s, len(s))

    async def state_loop(self):
        while True:
            await asyncio.sleep(10)
            self.broadcast_state()

    # ---------------------------------------------------------------- retention

    async def retention_loop(self):
        while True:
            try:
                self.apply_retention()
            except Exception as e:
                log("retention error:", e)
            await asyncio.sleep(300)

    def apply_retention(self):
        rec_dir = recordings_dir(self.cfg)
        files = sorted(glob.glob(os.path.join(rec_dir, "*.mp4")))
        cutoff = time.time() - float(self.cfg["retention_hours"]) * 3600
        for f in files:
            st = clip_start(f)
            if st is not None and st < cutoff:
                try:
                    os.remove(f)
                    log("deleted old clip", os.path.basename(f))
                except OSError:
                    pass
        # Low disk: delete oldest clips until there is room again.
        files = sorted(glob.glob(os.path.join(rec_dir, "*.mp4")))
        while files and shutil.disk_usage(rec_dir).free < float(self.cfg["min_free_gb"]) * 1e9:
            try:
                os.remove(files.pop(0))
            except OSError:
                break

    # ---------------------------------------------------------------- HTTP

    async def handle_http(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            if not line:
                return writer.close()
            method, target, _ = line.decode("latin-1").split(" ", 2)
            headers = {}
            while True:
                h = await asyncio.wait_for(reader.readline(), 15)
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            body = b""
            if "content-length" in headers:
                body = await reader.readexactly(min(int(headers["content-length"]), 1_000_000))
            if self.cfg.get("password") and not self.check_auth(headers):
                return await respond(writer, 401, b"Login required", "text/plain",
                                     {"WWW-Authenticate": 'Basic realm="OldPhone CCTV"'})
            url = urllib.parse.urlsplit(target)
            path = urllib.parse.unquote(url.path)
            query = urllib.parse.parse_qs(url.query)
            if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
                return await self.handle_ws(reader, writer, headers)
            if path == "/stream":
                return await self.handle_stream(reader, writer)
            if path == "/api/viewer" and method == "POST":
                return await self.handle_viewer_post(writer, query, headers, body)
            await self.route(writer, method, path, query, headers, body)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, ValueError):
            pass
        except Exception as e:
            log("http error:", repr(e))
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def check_auth(self, headers):
        a = headers.get("authorization", "")
        if not a.startswith("Basic "):
            return False
        try:
            _, _, pw = base64.b64decode(a[6:]).decode().partition(":")
        except Exception:
            return False
        return secrets.compare_digest(pw, self.cfg["password"])

    async def route(self, w, method, path, query, headers, body):
        if path == "/api/state":
            return await respond_json(w, self.state())
        if path == "/api/recordings":
            return await respond_json(w, self.list_recordings())
        if path.startswith("/rec/"):
            return await self.serve_clip(w, path[5:], headers, "download" in query)
        if path == "/api/export":
            return await self.export_range(w, query)
        if path == "/api/settings" and method == "POST":
            return await respond_json(w, await self.apply_settings(json.loads(body or b"{}")))
        if path == "/api/mode" and method == "POST":
            req = json.loads(body or b"{}")
            return await respond_json(w, await self.run_mode(req.get("mode"), bool(req.get("dry_run"))))
        if path == "/api/phone" and method == "POST":
            req = json.loads(body or b"{}")
            return await respond_json(w, await self.phone_action(req.get("action")))
        # static files
        if path == "/":
            path = "/index.html"
        f = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not f.startswith(WEB) or not os.path.isfile(f):
            return await respond(w, 404, b"Not found", "text/plain")
        ctype = mimetypes.guess_type(f)[0] or "application/octet-stream"
        if f.endswith(".js"):
            ctype = "text/javascript"
        with open(f, "rb") as fh:
            await respond(w, 200, fh.read(), ctype, {"Cache-Control": "no-cache"})

    async def serve_clip(self, w, name, headers, download):
        if "/" in name or "\\" in name or not name.endswith(".mp4"):
            return await respond(w, 404, b"Not found", "text/plain")
        f = os.path.join(recordings_dir(self.cfg), name)
        if not os.path.isfile(f):
            return await respond(w, 404, b"Not found", "text/plain")
        size = os.path.getsize(f)
        start, end, status = 0, size - 1, 200
        rng = headers.get("range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            elif b:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            status = 206
        if start > end:
            return await respond(w, 416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
        extra = {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}"}
        if download:
            extra["Content-Disposition"] = f'attachment; filename="{name}"'
        head = (f"HTTP/1.1 {status} {'Partial Content' if status == 206 else 'OK'}\r\n"
                f"Content-Type: video/mp4\r\nContent-Length: {end - start + 1}\r\nConnection: close\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in extra.items()) + "\r\n")
        w.write(head.encode())
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(262144, left))
                if not chunk:
                    break
                w.write(chunk)
                left -= len(chunk)
                await w.drain()

    async def export_range(self, w, query):
        """Cuts [start, end] (unix seconds) out of the clips into one MP4 download (no re-encode)."""
        try:
            start = float(query["start"][0]); end = float(query["end"][0])
        except (KeyError, ValueError):
            return await respond(w, 400, b"start and end required", "text/plain")
        if end <= start or end - start > 48 * 3600:
            return await respond(w, 400, b"bad range", "text/plain")
        clips = [c for c in self.list_recordings()["clips"] if not c["recording"] and c["end"] > start and c["start"] < end]
        if not clips:
            return await respond(w, 404, b"No recorded footage in that time range", "text/plain")
        ffmpeg = find_tool("ffmpeg", self.cfg.get("ffmpeg"))
        rec_dir = recordings_dir(self.cfg)
        tmp_dir = os.path.join(rec_dir, ".export")
        os.makedirs(tmp_dir, exist_ok=True)
        tag = secrets.token_hex(4)
        lst = os.path.join(tmp_dir, f"{tag}.txt")
        out = os.path.join(tmp_dir, f"{tag}.mp4")
        # Each file is trimmed to its own in/out point, so gaps in the footage never shift the cut.
        with open(lst, "w", encoding="utf-8") as f:
            for c in clips:
                p = os.path.join(rec_dir, c["name"]).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{p}'\n")
                if start > c["start"]:
                    f.write(f"inpoint {start - c['start']:.3f}\n")
                if end < c["end"]:
                    f.write(f"outpoint {end - c['start']:.3f}\n")
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out]
        code, msg = await run_cmd(args, timeout=600)
        try:
            if code != 0 or not os.path.isfile(out):
                return await respond(w, 500, ("Export failed: " + msg).encode(), "text/plain")
            name = "oldphonecctv_" + time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(max(start, clips[0]["start"]))) \
                   + "_to_" + time.strftime("%H-%M-%S", time.localtime(end)) + ".mp4"
            size = os.path.getsize(out)
            w.write((f"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: {size}\r\n"
                     f'Content-Disposition: attachment; filename="{name}"\r\nConnection: close\r\n\r\n').encode())
            with open(out, "rb") as fh:
                while chunk := fh.read(262144):
                    w.write(chunk)
                    await w.drain()
        finally:
            for p in (lst, out):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def list_recordings(self):
        rec_dir = recordings_dir(self.cfg)
        files = glob.glob(os.path.join(rec_dir, "*.mp4"))
        frags = sorted(f for f in files if f.endswith(FRAG_SUFFIX))
        live = frags[-1] if frags and self.recorder.active else None
        out = []
        for f in sorted(files):
            st = clip_start(f)
            if st is None:
                continue
            if f.endswith(FRAG_SUFFIX) and f != live:
                continue  # closed, about to be repacked by finalize_loop
            dur = None if f == live else mp4_duration(f)
            end = st + dur if dur else os.path.getmtime(f)
            out.append({"name": os.path.basename(f), "start": st, "end": end,
                        "size": os.path.getsize(f), "recording": f == live})
        return {"clips": out, "segment_minutes": self.cfg["segment_minutes"], "dir": rec_dir}

    async def finalize_loop(self):
        """Repacks closed fragmented clips into normal MP4s (lossless, about a second each)."""
        while True:
            try:
                await self.finalize_once()
            except Exception as e:
                log("finalize error:", repr(e))
            await asyncio.sleep(5)

    async def finalize_once(self):
        rec_dir = recordings_dir(self.cfg)
        frags = sorted(glob.glob(os.path.join(rec_dir, "*" + FRAG_SUFFIX)))
        if self.recorder.active and frags:
            frags = frags[:-1]          # the newest one is still being written
        ffmpeg = find_tool("ffmpeg", self.cfg.get("ffmpeg"))
        for f in frags:
            dst = f[: -len(FRAG_SUFFIX)] + ".mp4"
            tmp = dst + ".tmp"
            code, msg = await run_cmd([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", f,
                                       "-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp], timeout=300)
            if code == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, dst)
                os.remove(f)
            else:
                if os.path.exists(tmp):
                    os.remove(tmp)
                if os.path.getsize(f) < 4096 and time.time() - os.path.getmtime(f) > 60:
                    os.remove(f)        # empty clip from an instant stop
                else:
                    log("could not finalize", os.path.basename(f), msg.strip()[:200])

    async def apply_settings(self, req):
        phone = {k: req[k] for k in ("camera", "width", "height", "fps", "bitrate", "audio", "torch", "volume") if k in req}
        if phone:
            if not self.send_cmd({"cmd": "set", **phone}):
                return {"ok": False, "error": "phone not connected"}
        server_changed = False
        if "recordings_dir" in req:
            new = os.path.abspath(os.path.expandvars(os.path.expanduser(str(req["recordings_dir"]).strip().strip('"'))))
            try:
                os.makedirs(new, exist_ok=True)
                probe = os.path.join(new, ".oldphonecctv-write-test")
                with open(probe, "w") as f:
                    f.write("ok")
                os.remove(probe)
            except OSError as e:
                return {"ok": False, "error": f"Can't save recordings in {new}: {e.strerror or e}"}
            self.cfg["recordings_dir"] = new
            server_changed = True
            await self.recorder.stop()   # next keyframe starts a new clip in the new folder
            self.rec_restart = True
            self.request_key()
        for k in ("rotate", "record", "retention_hours", "name"):
            if k in req:
                self.cfg[k] = req[k]
                server_changed = True
        if server_changed:
            save_config(self.cfg)
            if "rotate" in req or "record" in req:
                self.rec_restart = True
                if not self.cfg["record"]:
                    await self.recorder.stop()
                self.request_key()
            self.broadcast_state()
        return {"ok": True}

    # ---------------------------------------------------------------- HTTP-stream viewers
    # Same messages as the WebSocket path, sent down one long chunked HTTP response as
    # [u32 length][message]. Works through antivirus web filters that break WebSockets.

    async def handle_stream(self, reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nCache-Control: no-store\r\n"
                     b"X-Content-Type-Options: nosniff\r\nX-Accel-Buffering: no\r\nTransfer-Encoding: chunked\r\n\r\n")
        await writer.drain()
        sink = StreamSink(writer)
        peer = writer.get_extra_info("peername")
        v = Viewer(sink, peer[0] if peer else "?")
        v.id = secrets.token_hex(8)
        self.stream_viewers[v.id] = v
        self.viewers.add(v)
        hello = json.dumps({"t": "viewer", "id": v.id})
        v.push(hello, len(hello))
        s = json.dumps(self.state())
        v.push(s, len(s))
        if self.config_annexb:
            v.push(bytes([1]) + self.config_annexb, len(self.config_annexb))
        self.request_key()
        self.broadcast_state()
        pump = asyncio.create_task(v.pump())
        try:
            # keep-alive so idle proxies don't cut the stream; ends when the browser goes away
            while not sink.closed and not pump.done():
                await asyncio.sleep(15)
                v.push("{\"t\":\"ping\"}", 12)
        finally:
            sink.closed = True
            pump.cancel()
            self.viewers.discard(v)
            self.stream_viewers.pop(v.id, None)
            if self.talking is v:
                self.talking = None
            self.broadcast_state()

    async def handle_viewer_post(self, writer, query, headers, body):
        v = self.stream_viewers.get((query.get("id") or [""])[0])
        if v is None:
            return await respond(writer, 410, b"gone", "text/plain")
        if headers.get("content-type", "").startswith("application/octet-stream"):
            if body:
                self.talking = v
                self.send_phone(T_TALK, body)
        else:
            self.viewer_command(v, json.loads(body or b"{}"))
        await respond(writer, 204, b"", "text/plain")

    def viewer_command(self, v, j):
        t = j.get("t")
        if t == "listen":
            v.listen = bool(j.get("on"))
        elif t == "talk":
            self.talking = v if j.get("on") else (None if self.talking is v else self.talking)
            self.broadcast_state()
        elif t == "key":
            v.need_key = True
            self.request_key()

    # ---------------------------------------------------------------- WebSocket viewers

    async def handle_ws(self, reader, writer, headers):
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC11B85").encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        ws = WebSocket(reader, writer)
        peer = writer.get_extra_info("peername")
        v = Viewer(ws, peer[0] if peer else "?")
        self.viewers.add(v)
        pump = asyncio.create_task(v.pump())
        s = json.dumps(self.state())
        v.push(s, len(s))
        if self.config_annexb:
            v.push(bytes([1]) + self.config_annexb, len(self.config_annexb))
        self.request_key()
        self.broadcast_state()
        try:
            while True:
                m = await ws.recv()
                if m is None:
                    break
                op, data = m
                if op == 2 and data and data[0] == T_TALK:
                    self.talking = v
                    self.send_phone(T_TALK, data[1:])
                elif op == 1:
                    self.viewer_command(v, json.loads(data))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            ws.closed = True
            self.viewers.discard(v)
            if self.talking is v:
                self.talking = None
            v.q.put_nowait((None, 0))
            pump.cancel()
            self.broadcast_state()

    # ---------------------------------------------------------------- phone modes / app control

    async def run_mode(self, mode, dry_run):
        if mode not in ("surveillance", "normal"):
            return {"ok": False, "error": "unknown mode"}
        if self.mode_busy:
            return {"ok": False, "error": "a mode change is already running"}
        self.mode_busy = True
        try:
            code, out = await asyncio.to_thread(switch_mode, self.cfg, mode, dry_run)
            return {"ok": code == 0, "output": out}
        finally:
            self.mode_busy = False

    async def phone_action(self, action):
        adb = find_tool("adb", self.cfg.get("adb"))
        if not adb:
            return {"ok": False, "output": "adb not found"}
        if action == "devices":
            code, out = await run_cmd([adb, "devices", "-l"])
            return {"ok": code == 0, "output": out}
        if action == "install":
            code, out = await asyncio.to_thread(install_phone, self.cfg)
            return {"ok": code == 0, "output": out}
        if action in ("start", "stop"):
            code, out = await run_cmd([adb, "shell", "am", "broadcast", "-n", f"{PHONE_PKG}/.ControlReceiver",
                                       "-a", f"{PHONE_PKG}.{action.upper()}"])
            return {"ok": code == 0, "output": out}
        return {"ok": False, "output": "unknown action"}

    # ---------------------------------------------------------------- main

    async def main(self):
        # Only one OldPhone CCTV may run: two would keep kicking each other off the phone.
        self._lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._lock.bind(("127.0.0.1", 47809))
            self._lock.listen(1)
        except OSError:
            log("OldPhone CCTV is already running on this PC - not starting a second copy.")
            log("Use 'Stop CCTV.bat' first if you want to restart it.")
            return
        loop = asyncio.get_running_loop()
        ep = self.start_beacon_listener(loop)
        if ep:
            await ep
        server = await asyncio.start_server(self.handle_http, self.cfg["bind"], int(self.cfg["http_port"]))
        log(f"OldPhone CCTV viewer: http://localhost:{self.cfg['http_port']}  (bound to {self.cfg['bind']})")
        log("recordings:", recordings_dir(self.cfg))
        if not find_tool("ffmpeg", self.cfg.get("ffmpeg")):
            log("WARNING: ffmpeg not found - live view works, recording is off until it is installed")
        await asyncio.gather(server.serve_forever(), self.phone_loop(), self.retention_loop(), self.state_loop(),
                             self.finalize_loop())


_duration_cache = {}


def mp4_duration(path):
    """Duration in seconds from the MP4's mvhd box (fast: no ffprobe). None if unreadable."""
    try:
        key = (path, os.path.getsize(path))
        if key in _duration_cache:
            return _duration_cache[key]
        with open(path, "rb") as f:
            def boxes(end):
                while f.tell() + 8 <= end:
                    start = f.tell()
                    size, kind = struct.unpack(">I4s", f.read(8))
                    if size == 1:
                        size = struct.unpack(">Q", f.read(8))[0]
                    elif size == 0:
                        size = end - start
                    yield kind, start, size
                    f.seek(start + size)
            total = os.path.getsize(path)
            for kind, start, size in boxes(total):
                if kind == b"moov":
                    f.seek(start + 8)
                    for k2, s2, _ in boxes(start + size):
                        if k2 == b"mvhd":
                            f.seek(s2 + 8)
                            ver = f.read(4)[0]
                            if ver == 1:
                                f.read(16); ts, dur = struct.unpack(">IQ", f.read(12))
                            else:
                                f.read(8); ts, dur = struct.unpack(">II", f.read(8))
                            d = dur / ts if ts and dur else None
                            _duration_cache[key] = d
                            return d
                    break
    except (OSError, struct.error):
        pass
    return None


def clip_start(path):
    try:
        return time.mktime(time.strptime(os.path.basename(path)[:19], "%Y-%m-%d_%H-%M-%S"))
    except ValueError:
        return None


async def respond(w, status, body, ctype, extra=None):
    reason = {200: "OK", 204: "No Content", 400: "Bad Request", 500: "Server Error", 401: "Unauthorized", 404: "Not Found", 410: "Gone",
              416: "Range Not Satisfiable"}.get(status, "OK")
    head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n" + "".join(f"{k}: {v}\r\n" for k, v in (extra or {}).items()) + "\r\n")
    w.write(head.encode() + body)
    await w.drain()


async def respond_json(w, obj):
    await respond(w, 200, json.dumps(obj).encode(), "application/json", {"Cache-Control": "no-store"})


async def run_cmd(args, timeout=60):
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT, **kw)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace")
    except Exception as e:
        return 1, str(e)


# --------------------------------------------------------------------------- adb helpers (sync, used by CLI + API)

def adb_cmd(cfg, *args, timeout=120):
    adb = find_tool("adb", cfg.get("adb"))
    if not adb:
        return 1, "adb not found (put platform-tools in tools\\platform-tools or install it)"
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        p = subprocess.run([adb, *args], capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "adb timed out"


def install_phone(cfg):
    """Install the camera app, grant its permissions, pair it with this PC and start it."""
    apk = os.path.join(ROOT, "phone", "OldPhoneCCTV.apk")
    lines = []
    steps = [
        ("install app", ["install", "-r", "-g", apk]),
        ("camera permission", ["shell", "pm", "grant", PHONE_PKG, "android.permission.CAMERA"]),
        ("mic permission", ["shell", "pm", "grant", PHONE_PKG, "android.permission.RECORD_AUDIO"]),
        ("keep running in background", ["shell", "dumpsys", "deviceidle", "whitelist", "+" + PHONE_PKG]),
        ("pairing key", ["shell", "am", "broadcast", "-n", f"{PHONE_PKG}/.ControlReceiver",
                         "-a", f"{PHONE_PKG}.SET_KEY", "--es", "key", cfg["phone_key"]]),
        ("start camera", ["shell", "am", "broadcast", "-n", f"{PHONE_PKG}/.ControlReceiver",
                          "-a", f"{PHONE_PKG}.START"]),
    ]
    worst = 0
    for name, args in steps:
        code, out = adb_cmd(cfg, *args)
        ok = code == 0 and "Failure" not in out and "Exception" not in out
        lines.append(f"{'OK  ' if ok else 'FAIL'} {name}" + ("" if ok else f": {out}"))
        if not ok and name in ("install app", "pairing key"):
            worst = 1
    return worst, "\n".join(lines)


def switch_mode(cfg, mode, dry_run=False):
    """Pushes the mode script to the phone and runs it there with nohup, so it finishes
    even if the adb connection drops while Wi-Fi or Bluetooth settings change."""
    script = os.path.join(ROOT, "phone", "modes", f"{mode}.sh")
    if not os.path.isfile(script):
        return 1, f"missing {script}"
    code, out = adb_cmd(cfg, "push", script, f"/data/local/tmp/oldphonecctv-{mode}.sh")
    if code:
        return code, "Phone not reachable over adb.\n" + out
    flag = " --dry-run" if dry_run else ""
    code, out = adb_cmd(cfg, "shell", f"sh /data/local/tmp/oldphonecctv-{mode}.sh{flag} 2>&1", timeout=300)
    return code, out


def cli():
    args = sys.argv[1:]
    if not args:
        app = App()
        try:
            asyncio.run(app.main())
        except KeyboardInterrupt:
            pass
        return
    cfg = load_config()
    if args[0] == "install-phone":
        code, out = install_phone(cfg)
        print(out)
        sys.exit(code)
    if args[0] == "mode" and len(args) >= 2:
        code, out = switch_mode(cfg, args[1], "--dry-run" in args)
        print(out)
        sys.exit(code)
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    cli()
