"""Video plumbing: H.264 helpers, a live Matroska muxer that feeds FFmpeg, the per-camera recorder,
clip finalising (fragmented -> normal MP4), clip listing and any-range export."""
import asyncio
import glob
import os
import secrets
import shutil
import struct
import time

FRAG_SUFFIX = ".rec.mp4"   # clip still being written (or left over from a crash)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def find_tool(name, configured, root):
    exe = name + (".exe" if os.name == "nt" else "")
    cands = []
    if configured:
        cands.append(configured)
    cands += [os.path.join(root, "tools", exe), os.path.join(root, "tools", "platform-tools", exe)]
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


async def run_cmd(args, timeout=60):
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT, **kw)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace")
    except Exception as e:
        return 1, str(e)


# --------------------------------------------------------------------------- H.264

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
    """Feeds one camera's stream to FFmpeg as live Matroska; FFmpeg cuts clock-aligned clips.
    Clips are fragmented while written so a crash or power cut never loses one."""

    def __init__(self, cam):
        self.cam = cam
        self.proc = None
        self.base_us = None
        self.cluster_ms = None
        self.has_audio = False
        self.pending = 0
        self.error = None

    @property
    def active(self):
        return self.proc is not None and self.proc.returncode is None

    async def start(self, config, has_audio, width, height, rotate, cfg, ffmpeg):
        if not ffmpeg:
            self.error = "FFmpeg not found - run setup.ps1 or put ffmpeg.exe in tools\\"
            return
        avcc = avcc_from_config(config)
        if not avcc:
            return
        os.makedirs(self.cam.dir, exist_ok=True)
        out = os.path.join(self.cam.dir, "%Y-%m-%d_%H-%M-%S" + FRAG_SUFFIX)
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        rot = int(rotate) % 360
        if rot:
            args += ["-display_rotation:v:0", str((360 - rot) % 360)]
        args += ["-f", "matroska", "-i", "pipe:0", "-map", "0:v"]
        if has_audio:
            args += ["-map", "0:a", "-c:a", "aac", "-b:a", cfg["audio_bitrate"], "-af", "aresample=async=1000"]
        args += ["-c:v", "copy", "-f", "segment", "-segment_time", str(int(cfg["segment_minutes"]) * 60),
                 "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
                 "-segment_format", "mp4", "-segment_format_options",
                 "movflags=+frag_keyframe+empty_moov+default_base_moof", out]
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        self.proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.PIPE, **kw)
        asyncio.create_task(self._read_stderr(self.proc))
        self.has_audio = has_audio
        self.base_us = None
        self.cluster_ms = None
        self.error = None
        self._write(mkv_header(avcc, width, height, 16000 if has_audio else 0))
        log(f"[{self.cam.name}] recording started ({width}x{height}{', audio' if has_audio else ''}, rotate {rot})")

    async def _read_stderr(self, proc):
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            msg = line.decode(errors="replace").strip()
            if msg:
                self.error = msg
                log(f"[{self.cam.name}] ffmpeg:", msg)

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
        log(f"[{self.cam.name}] recording stopped")


# --------------------------------------------------------------------------- clips on disk

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
                    if size < 8:
                        return
                    yield kind, start, size
                    f.seek(start + size)
            for kind, start, size in boxes(os.path.getsize(path)):
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
    except (OSError, struct.error, IndexError):
        pass
    return None


def clip_start(path):
    try:
        return time.mktime(time.strptime(os.path.basename(path)[:19], "%Y-%m-%d_%H-%M-%S"))
    except ValueError:
        return None


def list_clips(directory, recording_active):
    files = glob.glob(os.path.join(directory, "*.mp4"))
    frags = sorted(f for f in files if f.endswith(FRAG_SUFFIX))
    live = frags[-1] if frags and recording_active else None
    out = []
    for f in sorted(files):
        st = clip_start(f)
        if st is None:
            continue
        if f.endswith(FRAG_SUFFIX) and f != live:
            continue  # closed, about to be repacked by finalize_dir
        dur = None if f == live else mp4_duration(f)
        end = st + dur if dur else os.path.getmtime(f)
        out.append({"name": os.path.basename(f), "start": st, "end": end,
                    "size": os.path.getsize(f), "recording": f == live})
    return out


async def finalize_dir(directory, recording_active, ffmpeg):
    """Repack closed fragmented clips into normal fast-seeking MP4s (lossless, about a second each)."""
    frags = sorted(glob.glob(os.path.join(directory, "*" + FRAG_SUFFIX)))
    if recording_active and frags:
        frags = frags[:-1]          # the newest one is still being written
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


async def export_range(directory, clips, start, end, ffmpeg):
    """Cut [start, end] (unix seconds) from the clips into one MP4 (no re-encode).
    Returns (path, filename) or raises ValueError with a message for the user."""
    clips = [c for c in clips if not c["recording"] and c["end"] > start and c["start"] < end]
    if not clips:
        raise ValueError("No recorded footage in that time range")
    tmp_dir = os.path.join(directory, ".export")
    os.makedirs(tmp_dir, exist_ok=True)
    tag = secrets.token_hex(4)
    lst = os.path.join(tmp_dir, f"{tag}.txt")
    out = os.path.join(tmp_dir, f"{tag}.mp4")
    # Each file is trimmed to its own in/out point, so gaps in the footage never shift the cut.
    with open(lst, "w", encoding="utf-8") as f:
        for c in clips:
            p = os.path.join(directory, c["name"]).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{p}'\n")
            if start > c["start"]:
                f.write(f"inpoint {start - c['start']:.3f}\n")
            if end < c["end"]:
                f.write(f"outpoint {end - c['start']:.3f}\n")
    code, msg = await run_cmd([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                               "-i", lst, "-c", "copy", "-avoid_negative_ts", "make_zero",
                               "-movflags", "+faststart", out], timeout=600)
    os.remove(lst)
    if code != 0 or not os.path.isfile(out):
        raise ValueError("Export failed: " + msg.strip()[:300])
    name = (time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(max(start, clips[0]["start"])))
            + "_to_" + time.strftime("%H-%M-%S", time.localtime(end)) + ".mp4")
    return out, name
