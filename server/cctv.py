"""OldPhone CCTV - PC server. Turns old Android phones running the OldPhone CCTV app into CCTV cameras.

Standard library only (Python 3.9+). Run:  python server/cctv.py
Other commands:
    python server/cctv.py install-phone [serial]     install/refresh the app on adb phones and pair them
    python server/cctv.py mode surveillance [serial] strip a phone down to camera-only (reversible)
    python server/cctv.py mode normal [serial]       put the phone back exactly as it was
    add --dry-run to a mode command to only list what would change
"""
import asyncio
import base64
import glob
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alerts  # noqa: E402
from media import (FRAG_SUFFIX, Recorder, clip_start, export_range, finalize_dir, find_tool,  # noqa: E402
                   list_clips, log, run_cmd)

VERSION = "1.0.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
CONFIG_PATH = os.path.join(ROOT, "config.json")
PHONE_PKG = "com.oldphonecctv.camera"
PHONE_PORT = 47801
BEACON_PORT = 47800
LOCK_PORT = 47809

DEFAULTS = {
    "name": "OldPhone CCTV",
    "http_port": 8090,
    "bind": "127.0.0.1",          # localhost + `tailscale serve`; 0.0.0.0 opens it to the LAN
    "key": "",                     # pairing key shared with the phones (generated on first run)
    "recordings_dir": "recordings",
    "retention_hours": 48,
    "segment_minutes": 5,
    "audio_bitrate": "32k",
    "min_free_gb": 5,
    "password": "",                # optional HTTP Basic password (any user name)
    "armed": True,                 # send motion alerts
    "alerts": {"ntfy_url": "", "telegram_token": "", "telegram_chat": "", "cooldown_s": 60, "viewer_url": ""},
    "manual_hosts": [],            # phone IPs to connect to directly (e.g. over Tailscale), "ip" or "ip:port"
    "auto_discover": True,         # find phones on the LAN and over USB; false = only manual_hosts
    "cameras": {},                 # phone id -> {"name", "rotate", "record", "alerts"}
    "ffmpeg": "",
    "adb": "",
}
CAMERA_DEFAULTS = {"name": "", "rotate": 0, "record": True, "alerts": True}

T_HELLO, T_VIDEO, T_AUDIO, T_STATUS, T_MOTION, T_SNAPSHOT = 1, 2, 3, 4, 5, 6
T_TALK, T_CMD = 0x10, 0x11


# --------------------------------------------------------------------------- config

def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:  # tolerate a BOM from Notepad/PowerShell
            user = json.load(f)
        # migrate the single-phone prototype's settings
        if user.get("phone_key") and not user.get("key"):
            user["key"] = user.pop("phone_key")
        host = user.pop("phone_host", None)
        if host and host != "auto":
            user.setdefault("manual_hosts", []).append(host)
        for k in ("record", "rotate"):
            user.pop(k, None)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict) and k != "cameras":
                cfg[k].update(v)
            else:
                cfg[k] = v
    if not cfg.get("key"):
        cfg["key"] = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(12))
    save_config(cfg)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def recordings_root(cfg):
    d = cfg["recordings_dir"]
    if not os.path.isabs(d):
        d = os.path.join(ROOT, d)
    os.makedirs(d, exist_ok=True)
    return d


def safe_id(s):
    return re.sub(r"[^a-z0-9_-]", "", str(s).lower())[:32] or "camera"


# --------------------------------------------------------------------------- motion events

class Events:
    """Motion events in SQLite (stdlib). Snapshots are JPEG files next to each camera's clips."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, cam TEXT, start REAL, "
                        "end REAL, peak REAL, snap TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS ev_cam_start ON events(cam, start)")
        self.db.commit()

    def begin(self, cam, t, score):
        cur = self.db.execute("INSERT INTO events (cam, start, peak) VALUES (?, ?, ?)", (cam, t, score))
        self.db.commit()
        return cur.lastrowid

    def set_snap(self, eid, snap):
        self.db.execute("UPDATE events SET snap=? WHERE id=?", (snap, eid))
        self.db.commit()

    def finish(self, eid, t, peak):
        self.db.execute("UPDATE events SET end=?, peak=MAX(peak, ?) WHERE id=?", (t, peak, eid))
        self.db.commit()

    def close_open(self):
        """Events left open by a crash or disconnect end where they started (+30 s)."""
        self.db.execute("UPDATE events SET end=start+30 WHERE end IS NULL")
        self.db.commit()

    def list(self, cam, start, end):
        rows = self.db.execute("SELECT id, start, end, peak, snap FROM events WHERE cam=? AND start<? AND "
                               "COALESCE(end, start+30)>? ORDER BY start", (cam, end, start)).fetchall()
        return [{"id": r[0], "start": r[1], "end": r[2], "peak": r[3], "snap": r[4]} for r in rows]

    def purge(self, cutoff):
        rows = self.db.execute("SELECT cam, snap FROM events WHERE start<?", (cutoff,)).fetchall()
        self.db.execute("DELETE FROM events WHERE start<?", (cutoff,))
        self.db.commit()
        return rows


# --------------------------------------------------------------------------- viewers

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
    """Viewer messages as chunks of one long HTTP response: [u32 length][message]. Works through
    antivirus web filters that break WebSockets."""

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
    MAX_QUEUED = 6_000_000

    def __init__(self, sink, addr):
        self.sink = sink
        self.addr = addr
        self.id = secrets.token_hex(8)
        self.q = asyncio.Queue()
        self.queued = 0
        self.need_key = set()      # camera slots waiting for a keyframe
        self.watch = None          # None = all cameras, else a set of slots
        self.listen = None         # slot whose microphone this viewer hears

    def watching(self, slot):
        return self.watch is None or slot in self.watch

    def push(self, item, size, droppable=False):
        if droppable and self.queued > self.MAX_QUEUED:
            return False
        self.queued += size
        self.q.put_nowait((item, size))
        return True

    async def pump(self):
        while True:
            item, size = await self.q.get()
            self.queued -= size
            if item is None:
                return
            await self.sink.send(item, binary=not isinstance(item, str))


# --------------------------------------------------------------------------- one camera (phone)

class Camera:
    def __init__(self, app, cid, slot):
        self.app = app
        self.id = cid
        self.slot = slot
        self.writer = None
        self.target = None
        self.addr = None
        self.since = None
        self.hello = None
        self.status = None
        self.config_annexb = None
        self.recorder = Recorder(self)
        self.rec_restart = False
        self.last_key_request = 0
        self.talking = None
        self.error = None
        self.motion_on = False
        self.motion_event = None
        self.motion_started = 0
        self.last_alert = 0

    @property
    def cfg(self):
        c = self.app.cfg["cameras"].setdefault(self.id, dict(CAMERA_DEFAULTS))
        for k, v in CAMERA_DEFAULTS.items():
            c.setdefault(k, v)
        return c

    @property
    def name(self):
        return self.cfg.get("name") or (self.hello or {}).get("name") or self.id

    @property
    def dir(self):
        return os.path.join(recordings_root(self.app.cfg), self.id)

    @property
    def connected(self):
        return self.writer is not None and self.hello is not None

    def send(self, t, payload):
        w = self.writer
        if w is None:
            return False
        try:
            w.write(bytes([t]) + struct.pack(">I", len(payload)) + payload)
            return True
        except Exception:
            return False

    def cmd(self, obj):
        return self.send(T_CMD, json.dumps(obj).encode())

    def request_key(self):
        now = time.time()
        if now - self.last_key_request > 1:
            self.last_key_request = now
            self.cmd({"cmd": "key"})

    def video_size(self):
        try:
            wh = self.hello["video"].split(" ")[0].split("x")
            return int(wh[0]), int(wh[1])
        except Exception:
            return 1280, 720

    def public(self):
        rec = self.recorder
        return {
            "slot": self.slot, "id": self.id, "name": self.name, "connected": self.connected,
            "addr": self.addr, "since": self.since, "hello": self.hello, "status": self.status,
            "error": self.error, "rotate": self.cfg["rotate"], "alerts": self.cfg["alerts"],
            "rec": {"enabled": self.cfg["record"], "active": rec.active, "error": rec.error},
            "motion": self.motion_on, "talking": self.talking is not None,
        }

    # ---- messages from the phone

    async def on_message(self, t, data):
        if t == T_VIDEO:
            await self.on_video(data)
        elif t == T_AUDIO:
            self.on_audio(data)
        elif t == T_STATUS:
            self.status = json.loads(data)
            self.app.broadcast_state()
        elif t == T_HELLO:
            old = self.hello
            self.hello = json.loads(data)
            if old and (old.get("cfg", {}).get("audio") != self.hello.get("cfg", {}).get("audio")
                        or old.get("video") != self.hello.get("video")):
                self.rec_restart = True
            self.app.broadcast_state()
        elif t == T_MOTION:
            await self.on_motion(json.loads(data))
        elif t == T_SNAPSHOT:
            await self.on_snapshot(data)
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
            msg = bytes([1, self.slot]) + frame
            for v in self.app.viewers:
                if v.watching(self.slot):
                    v.push(msg, len(msg))
            return
        key = bool(flags & 1)
        want_rec = self.cfg["record"]
        if key and (self.rec_restart or (want_rec and not self.recorder.active)):
            self.rec_restart = False
            await self.recorder.stop()
            if want_rec and self.config_annexb:
                w, h = self.video_size()
                await self.recorder.start(self.config_annexb, bool(self.hello and self.hello.get("cfg", {}).get("audio")),
                                          w, h, self.cfg["rotate"], self.app.cfg, self.app.ffmpeg)
        self.recorder.video(pts, key, frame)
        msg = None
        for v in self.app.viewers:
            if not v.watching(self.slot):
                continue
            if self.slot in v.need_key:
                if not key:
                    self.request_key()
                    continue
                v.need_key.discard(self.slot)
            if msg is None:
                body = (self.config_annexb + frame) if key and self.config_annexb else frame
                msg = bytes([2, self.slot, 1 if key else 0]) + struct.pack(">q", pts) + body
            if not v.push(msg, len(msg), droppable=not key):
                v.need_key.add(self.slot)

    def on_audio(self, data):
        pts = struct.unpack(">q", data[:8])[0]
        pcm = data[8:]
        self.recorder.audio(pts, pcm)
        msg = None
        for v in self.app.viewers:
            if v.listen == self.slot and v is not self.talking:
                if msg is None:
                    msg = bytes([3, self.slot]) + pcm
                v.push(msg, len(msg), droppable=True)

    async def on_motion(self, j):
        now = time.time()
        if j.get("on"):
            self.motion_on = True
            self.motion_started = now
            self.motion_event = self.app.events.begin(self.id, now, float(j.get("score", 0)))
            log(f"[{self.name}] motion")
            # the snapshot follows right behind; alert from on_snapshot, or without a picture after 2 s
            eid = self.motion_event
            asyncio.get_running_loop().call_later(2, lambda: asyncio.ensure_future(self.alert(eid, None, late=True)))
        else:
            self.motion_on = False
            if self.motion_event:
                self.app.events.finish(self.motion_event, now, float(j.get("peak", 0)))
            self.motion_event = None
        self.app.broadcast_state()

    async def on_snapshot(self, jpeg):
        eid = self.motion_event
        if not eid:
            return
        snaps = os.path.join(self.dir, "snaps")
        os.makedirs(snaps, exist_ok=True)
        name = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(self.motion_started)) + f"_{eid}.jpg"
        with open(os.path.join(snaps, name), "wb") as f:
            f.write(jpeg)
        self.app.events.set_snap(eid, name)
        await self.alert(eid, jpeg)

    async def alert(self, eid, jpeg, late=False):
        if getattr(self, "_alerted", None) == eid:
            return
        if late and jpeg is None and self.app.events.db.execute("SELECT snap FROM events WHERE id=?", (eid,)).fetchone()[0]:
            return  # the snapshot path already alerted
        self._alerted = eid
        stamp = time.strftime("%H:%M:%S", time.localtime(self.motion_started))
        snap_url = f"/snap/{self.id}/" + (self.app.events.db.execute("SELECT snap FROM events WHERE id=?", (eid,)).fetchone()[0] or "")
        self.app.broadcast_json({"t": "motion", "cam": self.slot, "name": self.name, "time": self.motion_started,
                                 "snap": snap_url if jpeg else None})
        cfg = self.app.cfg
        if not (cfg["armed"] and self.cfg["alerts"]):
            return
        if time.time() - self.last_alert < float(cfg["alerts"].get("cooldown_s", 60)):
            return
        if not (cfg["alerts"].get("ntfy_url") or cfg["alerts"].get("telegram_token")):
            return
        self.last_alert = time.time()
        click = None
        base = cfg["alerts"].get("viewer_url", "").rstrip("/")
        if base:
            click = f"{base}/#rec={self.id}@{int(self.motion_started) - 5}"
        title = f"Motion: {self.name}"
        msg = f"Movement at {stamp} on {self.name}"
        results = await asyncio.to_thread(alerts.send_all, cfg["alerts"], title, msg, jpeg, click)
        for ok, m in results:
            if not ok:
                log(f"[{self.name}] alert failed: {m}")


# --------------------------------------------------------------------------- the app

class App:
    def __init__(self):
        self.cfg = load_config()
        self.cameras = {}            # id -> Camera
        self.viewers = set()
        self.stream_viewers = {}
        self.beacons = {}            # ip -> (time, info)
        self.links = {}              # target -> task
        self.suppressed = {}         # target -> until (a phone already reached another way)
        self.usb_ips = {}            # serial -> (time, wlan ip)
        self.usb_ports = {}          # serial -> local forward port
        self.mode_busy = False
        self._live_locks = {}
        self.ffmpeg = find_tool("ffmpeg", self.cfg.get("ffmpeg"), ROOT)
        self.adb = find_tool("adb", self.cfg.get("adb"), ROOT)
        self.events = Events(os.path.join(recordings_root(self.cfg), "events.db"))
        self.events.close_open()
        for cid in self.cfg["cameras"]:
            self.camera(cid)

    def camera(self, cid):
        cid = safe_id(cid)
        cam = self.cameras.get(cid)
        if cam is None:
            used = {c.slot for c in self.cameras.values()}
            slot = next(i for i in range(256) if i not in used)
            cam = self.cameras[cid] = Camera(self, cid, slot)
            if cid not in self.cfg["cameras"]:
                self.cfg["cameras"][cid] = dict(CAMERA_DEFAULTS)
                save_config(self.cfg)
            self.migrate_flat_clips(cam)
        return cam

    def by_slot(self, slot):
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return None
        return next((c for c in self.cameras.values() if c.slot == slot), None)

    def migrate_flat_clips(self, cam):
        """Single-camera prototype stored clips directly in recordings/; give them to the first camera."""
        root = recordings_root(self.cfg)
        flat = [f for f in glob.glob(os.path.join(root, "*.mp4")) if clip_start(f)]
        if flat and len(self.cfg["cameras"]) == 1:
            os.makedirs(cam.dir, exist_ok=True)
            for f in flat:
                shutil.move(f, os.path.join(cam.dir, os.path.basename(f)))
            log(f"moved {len(flat)} older clips to {cam.dir}")

    # ---------------------------------------------------------------- finding phones

    def start_beacon_listener(self, loop):
        app = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    j = json.loads(data)
                except Exception:
                    return
                if j.get("oldphonecctv") or j.get("homecam"):
                    where = f"{addr[0]}:{int(j.get('port', PHONE_PORT))}"
                    if where not in app.beacons:
                        log(f"found phone on Wi-Fi: {where} {j.get('name')}")
                    app.beacons[where] = (time.time(), j)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", BEACON_PORT))
            return loop.create_datagram_endpoint(Proto, sock=sock)
        except OSError as e:
            log("beacon listener unavailable:", e)
            return None

    async def usb_serials(self):
        if not self.adb:
            return []
        code, out = await run_cmd([self.adb, "devices"], timeout=8)
        if code != 0:
            return []
        return [l.split("\t")[0] for l in out.splitlines() if l.endswith("\tdevice")]

    async def usb_wlan_ip(self, serial):
        t, ip = self.usb_ips.get(serial, (0, None))
        if time.time() - t < 60:
            return ip
        code, out = await run_cmd([self.adb, "-s", serial, "shell", "ip -4 addr show wlan0"], timeout=8)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out or "")
        ip = m.group(1) if m else None
        self.usb_ips[serial] = (time.time(), ip)
        return ip

    async def desired_targets(self):
        now = time.time()
        live = {where for where, (t, _) in self.beacons.items() if now - t < 60}
        live_ips = {w.rsplit(":", 1)[0] for w in live}
        targets = set()
        for h in self.cfg.get("manual_hosts", []):
            targets.add(("host", h))
        if not self.cfg.get("auto_discover", True):
            return {t for t in targets if self.suppressed.get(t, 0) < now}
        for where in live:
            targets.add(("wifi", where))
        for serial in await self.usb_serials():
            ip = await self.usb_wlan_ip(serial)
            if ip and ip in live_ips:
                continue  # reachable on Wi-Fi already; one connection per phone
            targets.add(("usb", serial))
        # keep whatever a connected camera is using
        for cam in self.cameras.values():
            if cam.target:
                targets.add(cam.target)
        return {t for t in targets if self.suppressed.get(t, 0) < now}

    async def supervisor(self):
        while True:
            try:
                for t in await self.desired_targets():
                    task = self.links.get(t)
                    if task is None or task.done():
                        self.links[t] = asyncio.create_task(self.link(t))
            except Exception as e:
                log("supervisor:", repr(e))
            await asyncio.sleep(3)

    async def resolve(self, target):
        kind, val = target
        if kind == "usb":
            port = self.usb_ports.setdefault(val, 47811 + len(self.usb_ports))
            code, _ = await run_cmd([self.adb, "-s", val, "forward", f"tcp:{port}", f"tcp:{PHONE_PORT}"], timeout=8)
            return ("127.0.0.1", port) if code == 0 else (None, None)
        host, _, port = str(val).rpartition(":") if ":" in str(val) else (val, "", PHONE_PORT)
        return host, int(port)

    async def link(self, target):
        """One connection attempt to a phone; the supervisor retries while the target is wanted."""
        host, port = await self.resolve(target)
        if not host:
            await asyncio.sleep(3)
            return
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
        except Exception:
            await asyncio.sleep(3)
            return
        payload = json.dumps({"cmd": "auth", "key": self.cfg["key"]}).encode()
        w.write(bytes([T_CMD]) + struct.pack(">I", len(payload)) + payload)
        cam = None
        pinger = None
        try:
            while True:
                hdr = await asyncio.wait_for(r.readexactly(5), 20 if cam else 10)
                t, n = hdr[0], struct.unpack(">I", hdr[1:])[0]
                data = await asyncio.wait_for(r.readexactly(n), 20)
                if cam is None:
                    if t == T_STATUS and b"bad key" in data:
                        log(f"phone at {host} is paired with another PC - reset pairing on the phone to use it here")
                        self.suppressed[target] = time.time() + 120
                        return
                    if t != T_HELLO:
                        continue
                    hello = json.loads(data)
                    cam = self.camera(hello.get("id") or f"{hello.get('model', 'phone')}-{host}")
                    if cam.writer is not None and cam.writer is not w:
                        # The phone switched to this connection; stop the old path retrying.
                        if cam.target and cam.target != target:
                            self.suppressed[cam.target] = time.time() + 300
                        try:
                            cam.writer.close()
                        except Exception:
                            pass
                    cam.writer, cam.target, cam.since = w, target, time.time()
                    cam.addr = f"{host}:{port}" if target[0] != "usb" else f"USB {target[1]}"
                    cam.error = None
                    if hello.get("name") and not cam.cfg.get("name"):
                        cam.cfg["name"] = hello["name"]
                        save_config(self.cfg)
                    log(f"[{cam.name}] connected via {cam.addr} ({hello.get('model')}, {hello.get('video')})")
                    pinger = asyncio.create_task(self.ping(cam, w))
                    for v in self.viewers:
                        v.need_key.add(cam.slot)
                    await cam.on_message(t, data)
                    continue
                await cam.on_message(t, data)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, OSError):
            pass
        except Exception as e:
            log("link error:", repr(e))
        finally:
            if pinger:
                pinger.cancel()
            try:
                w.close()
            except Exception:
                pass
            if cam and cam.writer is w:
                cam.writer = None
                cam.hello = cam.config_annexb = None
                cam.status = None
                cam.since = None
                cam.error = "Lost the phone - reconnecting..."
                if cam.motion_event:
                    self.events.finish(cam.motion_event, time.time(), 0)
                    cam.motion_event = None
                cam.motion_on = False
                await cam.recorder.stop()
                log(f"[{cam.name}] disconnected")
                self.broadcast_state()
            await asyncio.sleep(2)

    async def ping(self, cam, w):
        while True:
            await asyncio.sleep(5)
            if cam.writer is w:
                cam.cmd({"cmd": "ping"})

    # ---------------------------------------------------------------- state for viewers

    def state(self):
        root = recordings_root(self.cfg)
        try:
            free_gb = round(shutil.disk_usage(root).free / 1e9, 1)
        except Exception:
            free_gb = None
        used = sum(os.path.getsize(f) for f in glob.glob(os.path.join(root, "*", "*.mp4")))
        a = self.cfg["alerts"]
        return {
            "t": "state",
            "name": self.cfg["name"],
            "version": VERSION,
            "cameras": [c.public() for c in sorted(self.cameras.values(), key=lambda c: c.slot)],
            "rec": {"dir": root, "used_gb": round(used / 1e9, 2), "free_gb": free_gb,
                    "retention_hours": self.cfg["retention_hours"], "segment_minutes": self.cfg["segment_minutes"]},
            "armed": self.cfg["armed"],
            "alerts": {"ntfy_url": a.get("ntfy_url", ""), "telegram_chat": a.get("telegram_chat", ""),
                       "telegram_set": bool(a.get("telegram_token")), "cooldown_s": a.get("cooldown_s", 60),
                       "viewer_url": a.get("viewer_url", "")},
            "viewers": len(self.viewers),
            "ffmpeg": bool(self.ffmpeg),
        }

    def broadcast_json(self, obj):
        s = json.dumps(obj)
        for v in self.viewers:
            v.push(s, len(s))

    def broadcast_state(self):
        self.broadcast_json(self.state())

    async def state_loop(self):
        while True:
            await asyncio.sleep(10)
            self.broadcast_state()

    # ---------------------------------------------------------------- housekeeping

    async def housekeeping_loop(self):
        tick = 0
        while True:
            try:
                for cam in list(self.cameras.values()):
                    if os.path.isdir(cam.dir) and self.ffmpeg:
                        await finalize_dir(cam.dir, cam.recorder.active, self.ffmpeg)
                if tick % 60 == 0:
                    self.apply_retention()
            except Exception as e:
                log("housekeeping:", repr(e))
            tick += 1
            await asyncio.sleep(5)

    def apply_retention(self):
        root = recordings_root(self.cfg)
        cutoff = time.time() - float(self.cfg["retention_hours"]) * 3600
        for f in glob.glob(os.path.join(root, "*", "*.mp4")):
            st = clip_start(f)
            if st is not None and st < cutoff and not f.endswith(FRAG_SUFFIX):
                try:
                    os.remove(f)
                except OSError:
                    pass
        for cam_id, snap in self.events.purge(cutoff):
            if snap:
                try:
                    os.remove(os.path.join(root, cam_id, "snaps", snap))
                except OSError:
                    pass
        # Low disk: oldest clips go first.
        files = sorted(glob.glob(os.path.join(root, "*", "*.mp4")), key=lambda f: clip_start(f) or 0)
        files = [f for f in files if not f.endswith(FRAG_SUFFIX)]
        while files and shutil.disk_usage(root).free < float(self.cfg["min_free_gb"]) * 1e9:
            try:
                os.remove(files.pop(0))
            except OSError:
                break

    # ---------------------------------------------------------------- HTTP

    async def handle_http(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            if not line:
                return
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
                body = await reader.readexactly(min(int(headers["content-length"]), 2_000_000))
            if self.cfg.get("password") and not self.check_auth(headers):
                return await respond(writer, 401, b"Login required", "text/plain",
                                     {"WWW-Authenticate": 'Basic realm="OldPhone CCTV"'})
            url = urllib.parse.urlsplit(target)
            path = urllib.parse.unquote(url.path)
            query = urllib.parse.parse_qs(url.query)
            if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
                return await self.handle_ws(reader, writer, headers)
            if path == "/stream":
                return await self.handle_stream(writer)
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

    def cam_from_query(self, query):
        cid = (query.get("cam") or [""])[0]
        return self.cameras.get(safe_id(cid)) if cid else None

    async def route(self, w, method, path, query, headers, body):
        if path == "/api/state":
            return await respond_json(w, self.state())
        if path == "/api/recordings":
            cam = self.cam_from_query(query)
            if not cam:
                return await respond_json(w, {"clips": []})
            clips = list_clips(cam.dir, cam.recorder.active) if os.path.isdir(cam.dir) else []
            return await respond_json(w, {"clips": clips, "segment_minutes": self.cfg["segment_minutes"]})
        if path == "/api/events":
            cam = self.cam_from_query(query)
            if not cam:
                return await respond_json(w, {"events": []})
            start = float((query.get("start") or [0])[0])
            end = float((query.get("end") or [time.time() + 60])[0])
            return await respond_json(w, {"events": self.events.list(cam.id, start, end)})
        if path.startswith("/rec/") or path.startswith("/snap/"):
            return await self.serve_media(w, path, headers, "download" in query)
        if path == "/api/export":
            return await self.export(w, query)
        if method == "POST":
            req = json.loads(body or b"{}") if not headers.get("content-type", "").startswith("application/octet") else None
            if path == "/api/viewer":
                return await self.viewer_post(w, query, headers, body)
            if path == "/api/settings":
                return await respond_json(w, await self.apply_settings(req))
            if path == "/api/alerts/test":
                return await respond_json(w, await self.test_alert())
            if path == "/api/camera/remove":
                return await respond_json(w, self.remove_camera(req.get("id")))
            if path == "/api/mode":
                return await respond_json(w, await self.run_mode(req.get("mode"), bool(req.get("dry_run")), req.get("serial")))
            if path == "/api/phone":
                return await respond_json(w, await self.phone_action(req.get("action"), req.get("serial")))
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

    async def serve_media(self, w, path, headers, download):
        parts = path.split("/")  # ['', 'rec'|'snap', cam, file]
        if len(parts) != 4:
            return await respond(w, 404, b"Not found", "text/plain")
        kind, cid, name = parts[1], safe_id(parts[2]), parts[3]
        if "/" in name or "\\" in name or ".." in name:
            return await respond(w, 404, b"Not found", "text/plain")
        root = recordings_root(self.cfg)
        if kind == "rec" and name.endswith(FRAG_SUFFIX):
            # The clip still being recorded: serve a playable copy of everything written so far.
            f, ctype = await self.live_clip_copy(os.path.join(root, cid, name)), "video/mp4"
            if not f:
                return await respond(w, 404, b"Not ready yet", "text/plain")
        elif kind == "rec" and name.endswith(".mp4"):
            f, ctype = os.path.join(root, cid, name), "video/mp4"
        elif kind == "snap" and name.endswith(".jpg"):
            f, ctype = os.path.join(root, cid, "snaps", name), "image/jpeg"
        else:
            return await respond(w, 404, b"Not found", "text/plain")
        if not os.path.isfile(f):
            return await respond(w, 404, b"Not found", "text/plain")
        await send_file(w, f, ctype, headers, name if download else None)

    async def live_clip_copy(self, src):
        """Remux the growing fragmented clip into a normal MP4 (lossless, ~1 s). Cached for 8 s so
        scrubbing doesn't re-run it on every request."""
        if not os.path.isfile(src) or not self.ffmpeg:
            return None
        out_dir = os.path.join(os.path.dirname(src), ".export")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, "live-" + os.path.basename(src).replace(FRAG_SUFFIX, ".mp4"))
        if os.path.isfile(out) and time.time() - os.path.getmtime(out) < 8:
            return out
        lock = self._live_locks.setdefault(out, asyncio.Lock())
        async with lock:
            if os.path.isfile(out) and time.time() - os.path.getmtime(out) < 8:
                return out
            tmp = out + ".tmp"
            code, _ = await run_cmd([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-c", "copy",
                                     "-movflags", "+faststart", "-f", "mp4", tmp], timeout=60)
            if code != 0 or not os.path.isfile(tmp):
                return out if os.path.isfile(out) else None
            os.replace(tmp, out)
            for old in glob.glob(os.path.join(out_dir, "live-*.mp4")):
                if old != out and time.time() - os.path.getmtime(old) > 60:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
        return out

    async def export(self, w, query):
        cam = self.cam_from_query(query)
        try:
            start = float(query["start"][0]); end = float(query["end"][0])
        except (KeyError, ValueError):
            return await respond(w, 400, b"start and end required", "text/plain")
        if not cam or end <= start or end - start > 48 * 3600:
            return await respond(w, 400, b"bad range", "text/plain")
        try:
            out, name = await export_range(cam.dir, list_clips(cam.dir, cam.recorder.active), start, end, self.ffmpeg)
        except ValueError as e:
            return await respond(w, 404, str(e).encode(), "text/plain")
        try:
            await send_file(w, out, "video/mp4", {}, f"{safe_id(cam.name)}_{name}")
        finally:
            try:
                os.remove(out)
            except OSError:
                pass

    async def apply_settings(self, req):
        cam = self.cameras.get(safe_id(req.get("cam", ""))) if req.get("cam") else None
        changed = False
        if cam:
            phone = {k: req[k] for k in ("camera", "width", "height", "fps", "bitrate", "audio", "torch", "volume",
                                         "motion", "sensitivity") if k in req}
            if "name" in req:
                cam.cfg["name"] = str(req["name"]).strip()[:40]
                phone["name"] = cam.cfg["name"]
                changed = True
            if phone and not cam.cmd({"cmd": "set", **phone}) and set(phone) - {"name"}:
                return {"ok": False, "error": "camera not connected"}
            for k in ("rotate", "record", "alerts"):
                if k in req:
                    cam.cfg[k] = req[k]
                    changed = True
            if "rotate" in req or "record" in req:
                cam.rec_restart = True
                if not cam.cfg["record"]:
                    await cam.recorder.stop()
                cam.request_key()
        else:
            if "recordings_dir" in req:
                new = os.path.abspath(os.path.expandvars(os.path.expanduser(str(req["recordings_dir"]).strip().strip('"'))))
                try:
                    os.makedirs(new, exist_ok=True)
                    probe = os.path.join(new, ".write-test")
                    with open(probe, "w") as f:
                        f.write("ok")
                    os.remove(probe)
                except OSError as e:
                    return {"ok": False, "error": f"Can't save recordings in {new}: {e.strerror or e}"}
                self.cfg["recordings_dir"] = new
                self.events = Events(os.path.join(new, "events.db"))
                for c in self.cameras.values():
                    await c.recorder.stop()
                    c.rec_restart = True
                    c.request_key()
                changed = True
            for k in ("retention_hours", "name", "armed"):
                if k in req:
                    self.cfg[k] = req[k]
                    changed = True
            if isinstance(req.get("alerts"), dict):
                for k in ("ntfy_url", "telegram_token", "telegram_chat", "cooldown_s", "viewer_url"):
                    if k in req["alerts"]:
                        self.cfg["alerts"][k] = req["alerts"][k]
                changed = True
        if changed:
            save_config(self.cfg)
            self.broadcast_state()
        return {"ok": True}

    async def test_alert(self):
        a = self.cfg["alerts"]
        if not (a.get("ntfy_url") or (a.get("telegram_token") and a.get("telegram_chat"))):
            return {"ok": False, "error": "Set up ntfy or Telegram first"}
        results = await asyncio.to_thread(alerts.send_all, a, "OldPhone CCTV test",
                                          "Alerts work. You'll get one like this when a camera sees movement.",
                                          None, a.get("viewer_url") or None)
        return {"ok": all(ok for ok, _ in results), "results": [m for _, m in results]}

    def remove_camera(self, cid):
        cam = self.cameras.get(safe_id(cid or ""))
        if not cam:
            return {"ok": False, "error": "unknown camera"}
        if cam.connected:
            return {"ok": False, "error": "camera is connected - stop the app on that phone first"}
        self.cameras.pop(cam.id, None)
        self.cfg["cameras"].pop(cam.id, None)
        save_config(self.cfg)
        self.broadcast_state()
        return {"ok": True}

    # ---------------------------------------------------------------- viewers (HTTP stream + WebSocket)

    def viewer_command(self, v, j):
        t = j.get("t")
        cam = self.by_slot(j.get("cam"))
        if t == "watch":
            cams = j.get("cams")
            v.watch = None if cams is None else {int(s) for s in cams}
            for c in self.cameras.values():
                if v.watching(c.slot):
                    v.need_key.add(c.slot)
                    if c.config_annexb:
                        v.push(bytes([1, c.slot]) + c.config_annexb, len(c.config_annexb) + 2)
                    c.request_key()
        elif t == "listen":
            v.listen = cam.slot if (cam and j.get("on")) else None
        elif t == "talk" and cam:
            if j.get("on"):
                cam.talking = v
            elif cam.talking is v:
                cam.talking = None
            self.broadcast_state()
        elif t == "key" and cam:
            v.need_key.add(cam.slot)
            cam.request_key()

    def add_viewer(self, v):
        self.viewers.add(v)
        v.push(json.dumps({"t": "viewer", "id": v.id}), 40)
        s = json.dumps(self.state())
        v.push(s, len(s))
        for c in self.cameras.values():
            v.need_key.add(c.slot)
            if c.config_annexb:
                v.push(bytes([1, c.slot]) + c.config_annexb, len(c.config_annexb) + 2)
            c.request_key()
        self.broadcast_state()

    def drop_viewer(self, v):
        self.viewers.discard(v)
        self.stream_viewers.pop(v.id, None)
        for c in self.cameras.values():
            if c.talking is v:
                c.talking = None
        self.broadcast_state()

    async def handle_stream(self, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nCache-Control: no-store\r\n"
                     b"X-Content-Type-Options: nosniff\r\nX-Accel-Buffering: no\r\nTransfer-Encoding: chunked\r\n\r\n")
        await writer.drain()
        sink = StreamSink(writer)
        peer = writer.get_extra_info("peername")
        v = Viewer(sink, peer[0] if peer else "?")
        self.stream_viewers[v.id] = v
        self.add_viewer(v)
        pump = asyncio.create_task(v.pump())
        try:
            while not sink.closed and not pump.done():
                await asyncio.sleep(15)
                v.push('{"t":"ping"}', 12)
        finally:
            sink.closed = True
            pump.cancel()
            self.drop_viewer(v)

    async def viewer_post(self, w, query, headers, body):
        v = self.stream_viewers.get((query.get("id") or [""])[0])
        if v is None:
            return await respond(w, 410, b"gone", "text/plain")
        if headers.get("content-type", "").startswith("application/octet-stream"):
            cam = self.by_slot((query.get("cam") or [None])[0])
            if cam and body:
                cam.talking = v
                cam.send(T_TALK, body)
        else:
            self.viewer_command(v, json.loads(body or b"{}"))
        await respond(w, 204, b"", "text/plain")

    async def handle_ws(self, reader, writer, headers):
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC11B85").encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        ws = WebSocket(reader, writer)
        peer = writer.get_extra_info("peername")
        v = Viewer(ws, peer[0] if peer else "?")
        self.add_viewer(v)
        pump = asyncio.create_task(v.pump())
        try:
            while True:
                m = await ws.recv()
                if m is None:
                    break
                op, data = m
                if op == 2 and len(data) > 2 and data[0] == T_TALK:
                    cam = self.by_slot(data[1])
                    if cam:
                        cam.talking = v
                        cam.send(T_TALK, data[2:])
                elif op == 1:
                    self.viewer_command(v, json.loads(data))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            ws.closed = True
            v.q.put_nowait((None, 0))
            pump.cancel()
            self.drop_viewer(v)

    # ---------------------------------------------------------------- phones over adb

    async def run_mode(self, mode, dry_run, serial):
        if mode not in ("surveillance", "normal"):
            return {"ok": False, "error": "unknown mode"}
        if self.mode_busy:
            return {"ok": False, "error": "a mode change is already running"}
        self.mode_busy = True
        try:
            code, out = await asyncio.to_thread(switch_mode, self.cfg, mode, dry_run, serial)
            return {"ok": code == 0, "output": out}
        finally:
            self.mode_busy = False

    async def phone_action(self, action, serial):
        if not self.adb:
            return {"ok": False, "output": "adb not found"}
        if action == "devices":
            code, out = await run_cmd([self.adb, "devices", "-l"])
            devs = []
            for l in out.splitlines()[1:]:
                if "\tdevice" in l or " device " in l:
                    sid = l.split()[0]
                    model = re.search(r"model:(\S+)", l)
                    devs.append({"serial": sid, "model": model.group(1).replace("_", " ") if model else sid})
            return {"ok": True, "devices": devs}
        if action == "install":
            code, out = await asyncio.to_thread(install_phone, self.cfg, serial)
            return {"ok": code == 0, "output": out}
        return {"ok": False, "output": "unknown action"}

    # ---------------------------------------------------------------- main

    async def main(self):
        # Only one server may run: two would keep taking the phones off each other.
        self._lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._lock.bind(("127.0.0.1", LOCK_PORT))
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
        log(f"OldPhone CCTV {VERSION}: http://localhost:{self.cfg['http_port']}  (bound to {self.cfg['bind']})")
        log("recordings:", recordings_root(self.cfg))
        if not self.ffmpeg:
            log("WARNING: ffmpeg not found - live view works, recording is off until it is installed")
        await asyncio.gather(server.serve_forever(), self.supervisor(), self.housekeeping_loop(), self.state_loop())


# --------------------------------------------------------------------------- HTTP helpers

async def respond(w, status, body, ctype, extra=None):
    reason = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
              410: "Gone", 416: "Range Not Satisfiable", 500: "Server Error"}.get(status, "OK")
    head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n" + "".join(f"{k}: {v}\r\n" for k, v in (extra or {}).items()) + "\r\n")
    w.write(head.encode() + body)
    await w.drain()


async def respond_json(w, obj):
    await respond(w, 200, json.dumps(obj).encode(), "application/json", {"Cache-Control": "no-store"})


async def send_file(w, f, ctype, headers, download_name=None):
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
    if download_name:
        extra["Content-Disposition"] = f'attachment; filename="{download_name}"'
    head = (f"HTTP/1.1 {status} {'Partial Content' if status == 206 else 'OK'}\r\n"
            f"Content-Type: {ctype}\r\nContent-Length: {end - start + 1}\r\nConnection: close\r\n"
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


# --------------------------------------------------------------------------- adb helpers (CLI + API)

def adb_cmd(cfg, *args, timeout=120):
    adb = find_tool("adb", cfg.get("adb"), ROOT)
    if not adb:
        return 1, "adb not found (put platform-tools in tools\\platform-tools or install it)"
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        p = subprocess.run([adb, *args], capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "adb timed out"


def adb_serials(cfg):
    code, out = adb_cmd(cfg, "devices")
    return [l.split("\t")[0] for l in out.splitlines() if l.endswith("\tdevice")] if code == 0 else []


def install_phone(cfg, serial=None):
    """Install the camera app on one adb phone (or all), grant permissions, pair it and start it."""
    apk = os.path.join(ROOT, "phone", "OldPhoneCCTV.apk")
    serials = [serial] if serial else adb_serials(cfg)
    if not serials:
        return 1, "No phone found on adb. Plug it in by USB with USB debugging on."
    lines, worst = [], 0
    for s in serials:
        lines.append(f"== {s}")
        steps = [
            ("install app", ["install", "-r", "-g", apk]),
            ("camera permission", ["shell", "pm", "grant", PHONE_PKG, "android.permission.CAMERA"]),
            ("mic permission", ["shell", "pm", "grant", PHONE_PKG, "android.permission.RECORD_AUDIO"]),
            ("keep running in background", ["shell", "dumpsys", "deviceidle", "whitelist", "+" + PHONE_PKG]),
            ("pair with this PC", ["shell", "am", "broadcast", "-n", f"{PHONE_PKG}/.ControlReceiver",
                                   "-a", f"{PHONE_PKG}.SET_KEY", "--es", "key", cfg["key"]]),
            ("start camera", ["shell", "am", "broadcast", "-n", f"{PHONE_PKG}/.ControlReceiver",
                              "-a", f"{PHONE_PKG}.START"]),
        ]
        for name, args in steps:
            code, out = adb_cmd(cfg, "-s", s, *args)
            ok = code == 0 and "Failure" not in out and "Exception" not in out
            lines.append(f"{'OK  ' if ok else 'FAIL'} {name}" + ("" if ok else f": {out}"))
            if not ok and name in ("install app", "pair with this PC"):
                worst = 1
    return worst, "\n".join(lines)


def switch_mode(cfg, mode, dry_run=False, serial=None):
    """Pushes the mode script to the phone and runs it there, so it finishes even if the adb
    connection drops while radios change."""
    script = os.path.join(ROOT, "phone", "modes", f"{mode}.sh")
    if not os.path.isfile(script):
        return 1, f"missing {script}"
    if not serial:
        serials = adb_serials(cfg)
        if len(serials) != 1:
            return 1, ("No phone on adb." if not serials else
                       "Several phones on adb - pick one: " + ", ".join(serials))
        serial = serials[0]
    code, out = adb_cmd(cfg, "-s", serial, "push", script, f"/data/local/tmp/oldphonecctv-{mode}.sh")
    if code:
        return code, "Phone not reachable over adb.\n" + out
    flag = " --dry-run" if dry_run else ""
    return adb_cmd(cfg, "-s", serial, "shell", f"sh /data/local/tmp/oldphonecctv-{mode}.sh{flag} 2>&1", timeout=300)


def cli():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if not args:
        app = App()
        try:
            asyncio.run(app.main())
        except KeyboardInterrupt:
            pass
        return
    if args[0] in ("--version", "version"):
        print(VERSION)
        return
    cfg = load_config()
    if args[0] == "install-phone":
        code, out = install_phone(cfg, args[1] if len(args) > 1 else None)
        print(out)
        sys.exit(code)
    if args[0] == "mode" and len(args) >= 2:
        code, out = switch_mode(cfg, args[1], "--dry-run" in sys.argv, args[2] if len(args) > 2 else None)
        print(out)
        sys.exit(code)
    if args[0] in ("--version", "version"):
        print(VERSION)
        return
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    cli()
