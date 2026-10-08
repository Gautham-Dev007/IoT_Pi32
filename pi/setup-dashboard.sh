#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }
[[ -f $HOME/.config/iothub/mqtt.env ]] || { echo "run setup-ota.sh first (it saves the MQTT login)"; exit 1; }

echo "Installing packages..."
sudo apt-get install -y -qq python3-flask python3-paho-mqtt python3-waitress >/dev/null

APP="$HOME/iothub-app"
mkdir -p "$APP/static"
install -m 644 dashboard/iothub.py "$APP/iothub.py"
install -m 644 dashboard/static/* "$APP/static/"
sudo install -m 755 iothub /usr/local/bin/iothub

DASH="$HOME/.config/iothub/dashboard.env"
if [[ ! -f $DASH ]]; then
  echo "Anyone with the link can VIEW the dashboard. Changing things needs the admin password."
  read -rsp "Admin password for the dashboard: " pw; echo
  printf 'DASH_PASSWORD=%q\nDASH_PORT=8080\n' "$pw" > "$DASH"; chmod 600 "$DASH"
fi

sudo tee /etc/systemd/system/iothub.service >/dev/null <<EOF
[Unit]
Description=IoT hub bridge and dashboard
After=network.target mosquitto.service

[Service]
User=$USER
WorkingDirectory=$APP
ExecStart=/usr/bin/python3 $APP/iothub.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable iothub >/dev/null 2>&1
sudo systemctl restart iothub
sleep 3
if systemctl is-active --quiet iothub; then
  echo
  echo "Dashboard is running:"
  echo "  same Wi-Fi : http://$(hostname -I | cut -d' ' -f1):8080"
  ts_ip=$(tailscale ip -4 2>/dev/null || true)
  [[ -n $ts_ip ]] && echo "  anywhere   : http://$ts_ip:8080   (Tailscale on)"
  echo "Logs: journalctl -u iothub -f"
else
  echo "Service failed to start. Logs:"; journalctl -u iothub -n 30 --no-pager
fi
