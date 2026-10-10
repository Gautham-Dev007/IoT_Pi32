#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }
CONF="$HOME/.config/iothub/weather.env"
mkdir -p "$(dirname "$CONF")"
[[ -f $CONF ]] && source "$CONF"

ask() { local v; read -rp "$1 [${2}]: " v; echo "${v:-$2}"; }
city=$(ask "Town or city where the sensors are" "${WEATHER_PLACE:-}")
[[ -n $city ]] || { echo "need a place name"; exit 1; }

mapfile -t HITS < <(python3 - "$city" <<'PY'
import json, sys, urllib.parse, urllib.request
q = urllib.parse.urlencode({"name": sys.argv[1], "count": 6, "language": "en", "format": "json"})
with urllib.request.urlopen(f"https://geocoding-api.open-meteo.com/v1/search?{q}", timeout=15) as r:
    for p in json.load(r).get("results") or []:
        label = ", ".join(x for x in (p.get("name"), p.get("admin1"), p.get("country")) if x)
        print(f"{p['latitude']}|{p['longitude']}|{label}")
PY
)
[[ ${#HITS[@]} -gt 0 ]] || { echo "No place called '$city' found. Try a bigger nearby city."; exit 1; }
for i in "${!HITS[@]}"; do echo "  $((i + 1))) ${HITS[$i]##*|}"; done
pick=$(ask "Which one" "1")
[[ $pick =~ ^[0-9]+$ && $pick -ge 1 && $pick -le ${#HITS[@]} ]] || { echo "pick a number from the list"; exit 1; }
IFS='|' read -r WEATHER_LAT WEATHER_LON label <<<"${HITS[$((pick - 1))]}"
WEATHER_PLACE=${label%%,*}

a=$(ask "Phone alert when rain looks likely soon? y/n" "${RAIN_ALERTS:-yes}");  [[ $a =~ ^[Yy] ]] && RAIN_ALERTS=yes || RAIN_ALERTS=no
a=$(ask "Phone tips (open a window, damp air, feels hot)? y/n" "${TIP_ALERTS:-yes}"); [[ $a =~ ^[Yy] ]] && TIP_ALERTS=yes || TIP_ALERTS=no
SUMMARY_TIME=$(ask "Send the day summary to your phone every day at (24 h, '-' for never)" "${SUMMARY_TIME:-21:00}")
[[ $SUMMARY_TIME == "-" ]] && SUMMARY_TIME=""
[[ -z $SUMMARY_TIME || $SUMMARY_TIME =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "time must look like 21:00"; exit 1; }

printf 'WEATHER_LAT=%q\nWEATHER_LON=%q\nWEATHER_PLACE=%q\nRAIN_ALERTS=%q\nTIP_ALERTS=%q\nSUMMARY_TIME=%q\n' \
  "$WEATHER_LAT" "$WEATHER_LON" "$WEATHER_PLACE" "$RAIN_ALERTS" "$TIP_ALERTS" "$SUMMARY_TIME" > "$CONF"
sudo systemctl restart iothub
sleep 6
curl -fsS "http://127.0.0.1:8080/api/weather" | python3 -c '
import json, sys
d = json.load(sys.stdin); c = d.get("current") or {}
if d.get("error"): print("Weather check failed:", d["error"])
elif c: print("Weather OK: %s C, %s%% and %s in %s." % (c.get("temp"), c.get("hum"), c.get("desc"), d.get("place")))
else: print("Weather is loading - check the dashboard in a minute.")
' || echo "Hub not answering yet - check the dashboard in a minute."
echo "Summary in your phone notifications: ${SUMMARY_TIME:-off}. Any time: iothub summary"
