# How it works

## Pieces

| Path | What |
|---|---|
| `android/` | The camera app, plain Java with no libraries. `build.ps1` builds it with aapt2/javac/d8/apksigner, no Gradle |
| `server/cctv.py` | Cameras, discovery and pairing, motion events, alerts, HTTP API, viewer streaming, adb helpers |
| `server/media.py` | H.264 helpers, live Matroska muxer, per-camera recorder, clip finalising, listing, export |
| `server/alerts.py` | ntfy and Telegram |
| `web/` | The viewer: one HTML page, no framework, no build step |
| `phone/modes/*.sh` | Surveillance / Normal mode, run on the phone over adb |
| `dev/fake_phone.py` | Simulated camera for development |

## Phone → PC protocol

One TCP connection per phone (port 47801 on the phone). Frames in both directions are
`[type u8][length u32 big-endian][payload]`.

| Type | Direction | Payload |
|---|---|---|
| 1 HELLO | phone→PC | JSON: id, name, model, settings, lenses, `motionAvailable` |
| 2 VIDEO | phone→PC | flags u8 (1 = keyframe, 2 = codec config) + pts i64 µs + H.264 Annex-B |
| 3 AUDIO | phone→PC | pts i64 µs + 40 ms of 16 kHz mono s16le |
| 4 STATUS | phone→PC | JSON every 2 s: battery, temp, fps, kbps, rssi, motion score, errors |
| 5 MOTION | phone→PC | JSON `{"on": true, "score"}` / `{"on": false, "peak"}` |
| 6 SNAPSHOT | phone→PC | JPEG taken when motion starts |
| 0x10 TALK | PC→phone | 16 kHz mono s16le to play on the speaker |
| 0x11 CMD | PC→phone | JSON: `auth`, `ping`, `key` (request keyframe), `set` (settings) |

The PC must send `{"cmd":"auth","key":…}` first. An unpaired phone stores that key (trust on first use). A paired
phone drops connections with any other key, and only switches to a new connection after it authenticates.

Phones announce themselves with a UDP broadcast on port 47800: `{"oldphonecctv":1,"id","name","port","paired"}`.

## Recording

Each camera's stream is wrapped in live Matroska (with real timestamps from the phone) and piped into FFmpeg,
which copies the video (no re-encoding), encodes audio to AAC and cuts clock-aligned 5-minute clips. Clips are
written as fragmented MP4 (`*.rec.mp4`), so a crash loses at most the last fragment. When a clip closes, the server
repacks it losslessly into a normal fast-start MP4. The clip still being recorded can be played: the server hands
out a fresh repacked copy on request.

Export concatenates clips with per-file in/out points (FFmpeg concat demuxer, stream copy), so cuts land within a
keyframe (2 s) of the chosen times and gaps in the footage never shift them.

## Motion detection

The app adds a small YUV stream (≥ 320 px wide) to the camera session. About 3 times a second it averages the
brightness of a 32×18 grid, subtracts the frame-wide brightness change (lights, clouds, auto-exposure), and counts
the cells that changed more than a threshold against a slowly adapting background. Two hits in a row start an event.
5 s without change ends it. Sensitivity (0–100) moves both the per-cell threshold and the share of the picture
that must change.

## Viewer

Live video is decoded in the browser with WebCodecs (`VideoDecoder`) and drawn to a canvas. Audio is plain PCM
through Web Audio. Talk-back captures the mic with an AudioWorklet, resamples it to 16 kHz and POSTs 120 ms chunks.
Everything server→browser travels down one long chunked HTTP response (`/stream`) as `[u32 length][message]`, which
gets through antivirus web filters that break WebSockets. A WebSocket endpoint (`/ws`) exists as well.
