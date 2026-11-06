# Setup guide

## 1. The PC

- Windows 10/11, on whenever you want recording. Any recent PC works: recording only copies streams, it
  doesn't re-encode them.
- Disk space: about **11 GB per camera per day** at the default 720p / 15 fps / 1 Mbps. Lower the bitrate in
  Settings to halve that.

Download the release zip, extract it somewhere permanent and run **`setup.ps1`** (right-click → Run with PowerShell).
If Windows blocks scripts, run `powershell -ExecutionPolicy Bypass -File .\setup.ps1`.

Setup does the following:
- checks for Python 3.9+ and installs 3.13 with winget if it's missing (if it installed Python, run setup once more);
- uses the bundled FFmpeg and adb;
- installs the camera app on any phone plugged in by USB with debugging on;
- sets up a private HTTPS address with Tailscale, if Tailscale is installed;
- can add OldPhone CCTV to Windows startup.

Start it with **`Start CCTV.bat`** and stop it with **`Stop CCTV.bat`**. Only one copy can run at a time. A second
one refuses to start, so two servers never fight over the phones.

## 2. The phone

Any Android 7.0+ phone with a working camera. Older and cheaper is fine.

**Without a cable:** copy `OldPhoneCCTV.apk` to the phone (or open it from a download link), allow installing
from that source, open **OldPhone CCTV** and tap **Start camera**. Allow camera and microphone.

**With a cable (recommended, no tapping needed):** turn on *Developer options → USB debugging*, plug the phone into
the PC and tap *Allow*. Then run `setup.ps1`, or Settings → *Phones on USB* → **Install / repair camera app**.

Tips:
- Tap **Allow running in the background** in the app (Android battery optimisation can otherwise pause it).
- Keep it on a charger in a cool place. The viewer shows battery % and temperature, turning amber at 45 °C or above.
- Optional: **Surveillance mode** (Settings → Phones on USB) for lower power. It's fully reversible with **Normal**.

### Pairing

A phone that has never been paired trusts the **first PC that connects** and refuses every other PC from then on.
To move a phone to another PC, tap **Reset pairing** in the app (or run
`adb shell am broadcast -n com.oldphonecctv.camera/.ControlReceiver -a com.oldphonecctv.camera.UNPAIR`).

## 3. Watching from anywhere (Tailscale)

1. Install [Tailscale](https://tailscale.com) on the PC and on your phone, signed in to the same account.
2. Run `setup.ps1` again. It runs `tailscale serve --bg <port>` and prints `https://<pc>.<tailnet>.ts.net`.
   The first time, Tailscale may print a link to enable HTTPS for your tailnet. Open it, enable, and run setup again.
3. Open that address on your phone and use **Add to Home screen**.

The server listens on `127.0.0.1` only, so it isn't reachable on your LAN or the internet except through
Tailscale. To open it on the LAN, set `"bind": "0.0.0.0"` in `config.json`, but browsers will then refuse live
video on plain `http://` from other devices. You can also add a password: `"password": "..."` (HTTP Basic login).

## 4. Motion alerts

**ntfy (recommended):** Settings → *Alerts to your phone* → **New** creates a random private topic, then press **Save**.
Install the ntfy app (Play Store, F-Droid, App Store), tap **+** and subscribe to the topic name (the part after
`ntfy.sh/`). Press **Send a test alert**. Each motion alert carries a snapshot, and tapping it opens the recording.
Set *Viewer address in alerts* to your Tailscale HTTPS address so the tap works away from home.
You can also use your own ntfy server: put its topic URL in the field.

**Telegram:** create a bot with @BotFather, put its token and your chat id in the Telegram section.

**Armed / Disarmed** (top bar) turns alerts on and off. Motion is still recorded on the timeline either way.
Each camera has its own *Motion detection*, *Sensitivity* and *Alerts from this camera* settings.

## config.json

Created on first run next to `setup.ps1`. Everything is editable in the viewer except:

| Key | Default | Meaning |
|---|---|---|
| `http_port` | 8090 | Viewer port |
| `bind` | 127.0.0.1 | 0.0.0.0 opens it to the LAN |
| `password` | "" | HTTP Basic password for the viewer |
| `manual_hosts` | [] | Phones to connect to directly, e.g. `"100.64.0.5"` (a phone on Tailscale) or `"ip:port"` |
| `auto_discover` | true | false = only connect to `manual_hosts` |
| `segment_minutes` | 5 | Length of the files on disk (the timeline hides them) |
| `min_free_gb` | 5 | Oldest footage is deleted when free space drops below this |
| `key` | random | Pairing key shared with the phones. Keep it private |
