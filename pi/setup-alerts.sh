#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }
CONF="$HOME/.config/iothub/alerts.env"
mkdir -p "$(dirname "$CONF")"
[[ -f $CONF ]] && source "$CONF"

ask() { local v; read -rp "$1 [${2}]: " v; echo "${v:-$2}"; }

echo "== ntfy (push notifications to the ntfy app) =="
default_topic=${NTFY_TOPIC:-iothub-$(python3 -c 'import secrets; print(secrets.token_hex(5))')}
echo "The topic name works like a password - anyone who knows it can read your alerts."
NTFY_TOPIC=$(ask "ntfy topic (Enter to accept, '-' to turn ntfy off)" "$default_topic")
[[ $NTFY_TOPIC == "-" ]] && NTFY_TOPIC=""

echo; echo "== Telegram (optional) =="
TELEGRAM_TOKEN=$(ask "Telegram bot token (Enter to skip)" "${TELEGRAM_TOKEN:-}")
TELEGRAM_CHAT_ID=""
[[ -n $TELEGRAM_TOKEN ]] && TELEGRAM_CHAT_ID=$(ask "Telegram chat id" "${TELEGRAM_CHAT_ID:-}")

echo; echo "== Limits (Enter keeps the value, '-' = no limit) =="
lim() { local v; v=$(ask "$1" "${2:--}"); [[ $v == "-" ]] && v=""; echo "$v"; }
TEMP_HIGH=$(lim "Alert if temperature above (C)" "${TEMP_HIGH:-35}")
TEMP_LOW=$(lim "Alert if temperature below (C)" "${TEMP_LOW:-}")
HUM_HIGH=$(lim "Alert if humidity above (%RH)" "${HUM_HIGH:-80}")
HUM_LOW=$(lim "Alert if humidity below (%RH)" "${HUM_LOW:-}")
OFFLINE_AFTER_MIN=$(ask "Node offline alert after (minutes)" "${OFFLINE_AFTER_MIN:-2}")
PI_TEMP_HIGH=$(ask "Pi CPU overheating alert above (C)" "${PI_TEMP_HIGH:-75}")

{
  printf 'NTFY_TOPIC=%q\nNTFY_SERVER=%q\n' "$NTFY_TOPIC" "${NTFY_SERVER:-https://ntfy.sh}"
  printf 'TELEGRAM_TOKEN=%q\nTELEGRAM_CHAT_ID=%q\n' "$TELEGRAM_TOKEN" "$TELEGRAM_CHAT_ID"
  printf 'TEMP_HIGH=%s\nTEMP_LOW=%s\nHUM_HIGH=%s\nHUM_LOW=%s\n' "$TEMP_HIGH" "$TEMP_LOW" "$HUM_HIGH" "$HUM_LOW"
  printf 'OFFLINE_AFTER_MIN=%s\nPI_TEMP_HIGH=%s\n' "$OFFLINE_AFTER_MIN" "$PI_TEMP_HIGH"
} > "$CONF"
chmod 600 "$CONF"

sudo systemctl restart iothub
echo
if [[ -n $NTFY_TOPIC ]]; then
  curl -fsS -H "Title: IoT Hub alerts set up" -H "Tags: white_check_mark" \
    -d "You'll get alerts here: power cuts, nodes offline, sensor faults, limits." \
    "${NTFY_SERVER:-https://ntfy.sh}/$NTFY_TOPIC" >/dev/null && echo "Sent a test message to ntfy."
  echo
  echo "On your phone: install the 'ntfy' app (Play Store / F-Droid / App Store),"
  echo "tap +, and subscribe to the topic:   $NTFY_TOPIC"
fi
echo "Settings saved in $CONF"
