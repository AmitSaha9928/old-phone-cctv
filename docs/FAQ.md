# FAQ and troubleshooting

**Which phones work?**
Android 7.0 or newer with a working camera. Tested daily on a Xiaomi Mi A2 (stock Android 10). Motion detection
needs the camera driver to run video plus a small second stream at once. Most phones can. If one can't, the app
turns motion off by itself and keeps streaming, and Settings tells you.

**How much power and heat?**
Encoding and motion detection run in the phone's hardware. Keep it on a charger, out of direct sun, and use
720p/15 fps. *Surveillance mode* also disables other apps and radios. The viewer shows the phone's temperature.

**How much disk space?**
About 11 GB per camera per day at 720p/15 fps/1 Mbps, about 5.5 GB at 0.5 Mbps. Retention is 1 to 14 days, and the
oldest footage goes first if free space drops below `min_free_gb`.

**The viewer says "Looking for cameras…".**
Make sure the phone and PC are on the same Wi-Fi and the app says *Camera on*. Allow Python through Windows Firewall
(Private networks) the first time, since that's how the PC hears the phone's "I'm here" signal. Some routers block
devices from seeing each other ("AP/client isolation"). In that case put the phone's IP in `manual_hosts` in
`config.json`, and reserve that IP for the phone in your router.

**"Phone at x.x.x.x is paired with another PC."**
The phone trusts the first PC that connected. Tap **Reset pairing** in the app to move it to this PC.

**Live video works on the PC but not on my phone.**
Use the `https://…ts.net` Tailscale address, not `http://<ip>`. Browsers only allow the live video decoder
(WebCodecs) and the microphone on secure pages.

**"Lost the connection to the server" in the browser, with the server running.**
Some antivirus web filters break WebSockets. The viewer uses plain HTTP streaming for this reason, so it usually
gets through. If it still fails, add `localhost` to the filter's exclusions.

**The phone's own Camera app shows the camera is busy.**
Only one app can use the camera at a time. Stop OldPhone CCTV (or use *Normal mode*) while you use the camera app.

**Can the PC turn a switched-off phone back on?**
No. A phone that's fully off has no network. The app does start by itself after a reboot, and the battery covers
power cuts. Many Qualcomm phones (not Xiaomi) can boot when the charger gets power with
`fastboot oem off-mode-charge 0`. Combined with a smart plug, that gives you a remote power button.

**Notes from bringing an old Mi A2 back**
- Custom ROMs are often why an old phone's camera fails: the camera driver crashes, or the camera service reports
  0 cameras. Reflashing the official firmware fixed it. Check `adb shell dumpsys media.camera` for
  "Number of camera devices".
- Some laptops' USB-3 ports fail to detect phones in fastboot mode ("Device Descriptor Request Failed"). A different
  port or a USB 2.0 hub helps, and fastboot then needs the Google USB driver assigned to the device by hand.
- Xiaomi's flash script stops at "Flashing is not allowed for Critical Partitions" until you run
  `fastboot flashing unlock_critical` and confirm on the phone.
- Samsung phones with a broken screen usually can't be set up: once locked, they block USB data entirely
  ("unlock to use USB device"). Fix the screen first.
