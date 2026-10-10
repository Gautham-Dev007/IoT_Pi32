#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }

mkdir -p "$HOME/firmware" "$HOME/.config/iothub"

CONF="$HOME/.config/iothub/mqtt.env"
if [[ ! -f $CONF ]]; then
  read -rp "MQTT username [esp]: " u; u=${u:-esp}
  read -rsp "MQTT password: " p; echo
  printf 'MQTT_USER=%q\nMQTT_PASS=%q\n' "$u" "$p" > "$CONF"
  chmod 600 "$CONF"
fi

sudo install -m 755 ota-push /usr/local/bin/ota-push

sudo tee /etc/systemd/system/iothub-firmware.service >/dev/null <<EOF
[Unit]
Description=IoT hub firmware server for ESP32 OTA
After=network.target

[Service]
User=$USER
ExecStart=/usr/bin/python3 -m http.server 8000 --directory $HOME/firmware
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now iothub-firmware
sleep 1
systemctl is-active iothub-firmware >/dev/null && echo "Firmware server running on port 8000"

source "$CONF"
if mosquitto_pub -h localhost -u "$MQTT_USER" -P "$MQTT_PASS" -t home/test -m setup-check; then
  echo "MQTT login OK"
else
  echo "MQTT login FAILED - fix $CONF"
fi
echo "Done. Usage: ota-push <node-id> <firmware.bin>"
