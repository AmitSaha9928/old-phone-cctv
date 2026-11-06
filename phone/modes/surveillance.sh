#!/system/bin/sh
# OldPhone CCTV - Surveillance mode. Runs on the phone (adb shell). Fully reversible with normal.sh:
# every app it disables and every setting it changes is written to /data/local/tmp/oldphonecctv-mode/
# first, and normal.sh puts exactly those back. Nothing is uninstalled, no data is deleted.
DRY=0
[ "$1" = "--dry-run" ] && DRY=1
D=/data/local/tmp/oldphonecctv-mode
mkdir -p $D
CAM=com.oldphonecctv.camera

say() { echo "$@"; }
run() { if [ $DRY = 1 ]; then say "  would run: $*"; else "$@" >/dev/null 2>&1; fi; }

# Apps that must keep working: the camera app, the system, Google services, keyboard, launcher,
# Tailscale, and whatever app currently provides WebView.
WEBVIEW=$(dumpsys webviewupdate 2>/dev/null | sed -n 's/.*Current WebView package (name, version): (\([^,]*\),.*/\1/p')
KEEP=" $CAM com.tailscale.ipn $WEBVIEW com.google.android.webview com.android.webview com.android.chrome "
is_kept() {
  case "$KEEP" in *" $1 "*) return 0;; esac
  case "$1" in
    *inputmethod*|*launcher*|*keyboard*|com.android.*|android|com.qualcomm.*|com.qti.*|org.codeaurora.*|vendor.*) return 0;;
    com.google.android.gms|com.google.android.gsf|com.android.vending|com.google.android.ext.*|com.google.android.permissioncontroller) return 0;;
  esac
  return 1
}

# Heavy pre-installed apps that are safe to switch off on a camera-only phone (skipped if absent).
EXTRA="com.google.android.youtube com.google.android.apps.youtube.music com.google.android.apps.photos
com.google.android.gm com.google.android.apps.docs com.google.android.apps.docs.editors.docs
com.google.android.apps.docs.editors.sheets com.google.android.apps.docs.editors.slides
com.google.android.videos com.google.android.music com.google.android.apps.maps com.google.android.apps.tachyon
com.google.android.googlequicksearchbox com.google.android.apps.googleassistant com.google.android.calendar
com.google.android.keep com.google.android.apps.wellbeing com.google.android.apps.podcasts
com.google.ar.lens com.google.android.apps.subscriptions.red com.google.android.feedback
com.google.android.apps.nbu.files com.google.android.apps.magazines com.google.android.apps.fitness
com.google.android.apps.walletnfcrel com.google.android.apps.restore com.google.android.marvin.talkback
com.google.android.apps.messaging com.google.android.contacts com.google.android.dialer
com.mi.android.globalFileexplorer com.xiaomi.midrop com.xiaomi.scanner com.mi.globalbrowser
com.miui.calculator com.android.camera com.mfashiongallery.emag com.xiaomi.glgm com.facebook.appmanager
com.facebook.services com.facebook.system com.netflix.partner.activation"

ENABLED=$(pm list packages -e 2>/dev/null | sed 's/^package://')
is_enabled() { echo "$ENABLED" | grep -qx "$1"; }

say "== Apps"
COUNT=0
for p in $( (pm list packages -3 -e 2>/dev/null | sed 's/^package://'; echo "$EXTRA" | tr ' ' '\n') | sort -u); do
  is_enabled "$p" || continue
  is_kept "$p" && continue
  grep -qx "$p" $D/disabled.txt 2>/dev/null && continue
  if [ $DRY = 1 ]; then
    say "  would disable $p"
  else
    if pm disable-user --user 0 "$p" 2>&1 | grep -q "disabled-user"; then
      echo "$p" >> $D/disabled.txt
      say "  disabled $p"
    else
      say "  could not disable $p (left as is)"
    fi
  fi
  COUNT=$((COUNT + 1))
done
say "  $COUNT app(s)"

# ---- settings: remember the original value once, then change it
setting() { # namespace key value
  old=$(settings get "$1" "$2" 2>/dev/null)
  if [ $DRY = 1 ]; then say "  would set $1 $2: $old -> $3"; return; fi
  grep -q "^$1 $2 " $D/settings.txt 2>/dev/null || echo "$1 $2 $old" >> $D/settings.txt
  settings put "$1" "$2" "$3"
  say "  $1 $2: $old -> $3"
}

say "== Settings"
setting global stay_on_while_plugged_in 0
setting system screen_off_timeout 15000
setting system screen_brightness_mode 0
setting system screen_brightness 5
setting global window_animation_scale 0
setting global transition_animation_scale 0
setting global animator_duration_scale 0
setting global wifi_scan_always_enabled 0
setting global ble_scan_always_enabled 0
setting global wifi_sleep_policy 2
setting global mobile_data_always_on 0
setting secure location_mode 0

say "== Radios"
if [ $DRY = 1 ]; then
  say "  would turn off mobile data (was $(settings get global mobile_data)) and Bluetooth (was $(settings get global bluetooth_on))"
else
  [ -f $D/radios.txt ] || echo "mobile_data $(settings get global mobile_data)
bluetooth_on $(settings get global bluetooth_on)" > $D/radios.txt
  svc data disable >/dev/null 2>&1
  svc bluetooth disable >/dev/null 2>&1 || cmd bluetooth_manager disable >/dev/null 2>&1
  cmd location set-location-enabled false >/dev/null 2>&1
  say "  mobile data off, Bluetooth off, location off (Wi-Fi stays on)"
fi

say "== Camera"
run dumpsys deviceidle whitelist +$CAM
run am set-standby-bucket $CAM active
run am broadcast -n $CAM/.ControlReceiver -a $CAM.START
run input keyevent KEYCODE_SLEEP
[ $DRY = 1 ] || { echo surveillance > $D/mode; say "  camera running, screen off"; }
say "Surveillance mode $( [ $DRY = 1 ] && echo 'preview finished (nothing changed)' || echo on )."
