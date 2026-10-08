#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }
CONF="$HOME/.config/iothub/adafruit.env"

echo "Find these at https://io.adafruit.com -> the yellow key icon (My Key)."
read -rp "Adafruit IO username: " user
read -rsp "Adafruit IO key (starts with aio_): " key; echo
[[ -n $user && -n $key ]] || { echo "both are needed"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-AIO-Key: $key" "https://io.adafruit.com/api/v2/$user/feeds")
case $code in
  200) echo "Login OK";;
  401|403) echo "Adafruit IO rejected that username/key (HTTP $code)"; exit 1;;
  *) echo "Couldn't check the key (HTTP $code) - saving anyway";;
esac

read -rp "Also upload the Pi's CPU temperature? (uses 1 of your 10 free feeds) [y/N]: " pi
[[ ${pi:-n} =~ ^[Yy] ]] && pi=yes || pi=no
printf 'AIO_USERNAME=%q\nAIO_KEY=%q\nAIO_INCLUDE_PI=%s\n' "$user" "$key" "$pi" > "$CONF"
chmod 600 "$CONF"

sudo systemctl restart iothub
echo
echo "Done. Within a minute your feeds appear at https://io.adafruit.com/$user/feeds"
echo "Make a dashboard there: Dashboards -> New -> add Line Chart / Gauge blocks and pick the iothub feeds."
