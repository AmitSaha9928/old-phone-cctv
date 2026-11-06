# OldPhone CCTV: Mi A2 as a CCTV camera

A phone app streams the camera and microphone to a PC. The PC shows it live in a web page, records
compressed MP4 clips that are kept for 2 days, and lets you talk through the phone's speaker. It works
from your own phone too, through Tailscale.

```
 Mi A2 (OldPhone CCTV app)  --Wi-Fi or USB-->  PC: server/cctv.py  --> recordings\*.mp4 (5-min clips, 48 h)
   camera + mic  ->  H.264 + PCM                    |
   speaker      <-  talk audio                      +--> web viewer :8090  --> tailscale serve (HTTPS) --> your phone
```

## Moving to your PC
Step-by-step with troubleshooting: **[SETUP-ON-YOUR-PC.md](SETUP-ON-YOUR-PC.md)**. Short version:
1. Copy this whole folder (it already contains FFmpeg and adb in `tools\`). The `android-src\` and
   `.claude\` folders are only needed for rebuilding the app, so they can be left out.
2. Right-click `setup.ps1` → **Run with PowerShell**. It checks for Python (installs it with winget if
   missing), installs/pairs the phone app if the phone is plugged in, sets up the Tailscale HTTPS address
   and can add OldPhone CCTV to Windows startup.
3. Double-click **Start CCTV.bat** → http://localhost:8090

`config.json` holds the pairing key that the phone app was given. Keep it with the folder. If it's lost, plug
the phone in and run `py -3 server\cctv.py install-phone` to pair again.

## Daily use
- **Live**: picture, Listen (phone mic), **hold to talk** (or tap once to keep talking), snapshot, flashlight.
- **Camera switch** under the live picture: Rear (12 MP) / Front (20 MP). The Mi A2's two extra rear
  sensors can only be opened by Xiaomi's own camera app, so they aren't offered.
- **Recordings**: one continuous timeline per day. Tap or drag to scrub, zoom 24h / 6h / 1h / 10m
  (mouse wheel pans when zoomed), play at up to 16×, ←/→ to step. Playback runs straight across files.
- **Save footage**: set From/To (or Shift+drag on the timeline, or "Use playhead") and download that
  exact stretch as one MP4. Cuts land on the nearest keyframe (every 2 s).
- **Settings**: lens, quality, frame rate, bitrate, rotation, mic, speaker volume, recording on/off,
  **where recordings are saved**, and how long they're kept. After changing the folder, new footage goes
  there; earlier footage stays in the old folder (move it over if you want it on the timeline).

Recording is crash-safe: footage is written in 2-second fragments, so a power cut loses at most the
last few seconds. Each 5-minute file is repacked into a normal MP4 when it closes.
- **Phone mode**:
  - *Surveillance* disables other apps (not deleted), turns off Bluetooth, location and mobile data, dims
    the screen and starts the camera.
  - *Normal* restores exactly what Surveillance changed (it keeps a record on the phone in
    `/data/local/tmp/oldphonecctv-mode/`) and stops the camera.
  - Both need the phone on adb (USB, or wireless debugging). Use *Preview changes* to see the list first.

## From your phone (Tailscale)
Live video and talking need a secure (https) page, so use the address `setup.ps1` prints
(`https://<pc-name>.<tailnet>.ts.net`). `tailscale serve` keeps it private to your tailnet. On the phone,
"Add to Home screen" makes it open like an app.

## Storage
At the default 720p / 15 fps / 1 Mbps, a day is about 10-11 GB, so 2 days ≈ 21 GB. 0.5 Mbps halves that.
Clips older than the retention period are deleted every 5 minutes. If free space drops below 5 GB,
the oldest clips go first.

## Power tips for the phone
- Keep it on a charger, somewhere cool. The viewer shows battery % and temperature (amber ≥ 45 °C).
- Surveillance mode plus 720p/15 fps is the lowest-power setup. The screen can stay off.
- The app restarts on its own after a reboot or power cut.

## Turning the phone on when it's off
A powered-off phone can't be woken over the network. Tested on this Mi A2 (stock Android 10):
- **After any reboot or crash**, OldPhone CCTV starts by itself (verified).
- **Power cuts**: the battery keeps the phone and camera running; keep it charged.
- **Boot-when-powered doesn't work on the Mi A2.** `fastboot oem off-mode-charge 0` is accepted but
  Xiaomi's bootloader ignores it: a fully off phone shows the charging battery and needs one press of the
  power button. (It works on many other Qualcomm phones, e.g. Pixels and Motorolas.)

## Files
| Path | What |
|---|---|
| `server/cctv.py` | PC server (Python standard library only) |
| `web/` | viewer page |
| `phone/OldPhoneCCTV.apk` | phone app; `phone/modes/*.sh` surveillance/normal scripts |
| `tools/` | bundled `ffmpeg.exe` and adb |
| `android-src/` | app source; `build.ps1` rebuilds the APK (needs the Android SDK) |
| `config.json` | settings + phone pairing key (created on first run) |
| `recordings/` | the clips |
