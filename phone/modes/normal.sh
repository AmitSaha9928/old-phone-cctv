#!/system/bin/sh
# OldPhone CCTV - Normal mode. Undoes exactly what surveillance.sh changed, using the records it left in
# /data/local/tmp/oldphonecctv-mode/, then stops the camera so the phone's own camera app works again.
DRY=0
[ "$1" = "--dry-run" ] && DRY=1
D=/data/local/tmp/oldphonecctv-mode
CAM=com.oldphonecctv.camera

if [ ! -d $D ]; then
  echo "Phone was not in Surveillance mode - nothing to restore."
else
  echo "== Apps"
  if [ -f $D/disabled.txt ]; then
    while read -r p; do
      [ -z "$p" ] && continue
      if [ $DRY = 1 ]; then echo "  would enable $p"; else pm enable --user 0 "$p" >/dev/null 2>&1 && echo "  enabled $p"; fi
    done < $D/disabled.txt
  fi

  echo "== Settings"
  if [ -f $D/settings.txt ]; then
    while read -r ns key val; do
      [ -z "$key" ] && continue
      if [ $DRY = 1 ]; then echo "  would restore $ns $key -> $val"; continue; fi
      if [ "$val" = "null" ] || [ -z "$val" ]; then settings delete "$ns" "$key" >/dev/null 2>&1
      else settings put "$ns" "$key" "$val"; fi
      echo "  $ns $key -> $val"
    done < $D/settings.txt
  fi

  echo "== Radios"
  if [ -f $D/radios.txt ] && [ $DRY = 0 ]; then
    [ "$(grep '^mobile_data ' $D/radios.txt | cut -d' ' -f2)" = "1" ] && svc data enable >/dev/null 2>&1 && echo "  mobile data on"
    [ "$(grep '^bluetooth_on ' $D/radios.txt | cut -d' ' -f2)" = "1" ] && { svc bluetooth enable >/dev/null 2>&1 || cmd bluetooth_manager enable >/dev/null 2>&1; } && echo "  Bluetooth on"
  fi
  if [ $DRY = 0 ]; then
    cmd location set-location-enabled true >/dev/null 2>&1
    rm -rf $D
  fi
fi

echo "== Camera"
if [ $DRY = 1 ]; then echo "  would stop the OldPhone CCTV camera"; else
  am broadcast -n $CAM/.ControlReceiver -a $CAM.STOP >/dev/null 2>&1
  echo "  camera stopped (start it again from the viewer or the OldPhone CCTV app)"
fi
echo "Normal mode $( [ $DRY = 1 ] && echo 'preview finished (nothing changed)' || echo restored )."
