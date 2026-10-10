#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }
systemctl is-active --quiet NetworkManager || { echo "NetworkManager is not running - this needs it"; exit 1; }

SSID="IoTHub"
read -rsp "Password for the IoTHub Wi-Fi (min 8 chars): " PASS; echo
[[ ${#PASS} -ge 8 ]] || { echo "password must be at least 8 characters"; exit 1; }

sudo apt-get install -y -qq dnsmasq-base >/dev/null

sudo nmcli con delete iothub-ap >/dev/null 2>&1 || true
sudo nmcli con add type wifi ifname wlan0 con-name iothub-ap autoconnect no ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
  wifi-sec.psk "$PASS" >/dev/null
echo "Created Wi-Fi access point profile '$SSID'"

sudo tee /etc/NetworkManager/dispatcher.d/90-iothub-ap >/dev/null <<'EOF'
#!/bin/sh
# Start the IoTHub AP when Ethernet comes up, stop it when Ethernet goes down.
[ "$1" = "eth0" ] || exit 0
case "$2" in
  up)   logger -t iothub-ap "eth0 up - starting IoTHub AP";  nmcli con up iothub-ap ;;
  down) logger -t iothub-ap "eth0 down - stopping IoTHub AP"; nmcli con down iothub-ap ;;
esac
exit 0
EOF
sudo chmod 755 /etc/NetworkManager/dispatcher.d/90-iothub-ap

if [[ $(cat /sys/class/net/eth0/carrier 2>/dev/null || echo 0) == 1 ]]; then
  echo "Ethernet is connected - starting the AP now"
  sudo nmcli con up iothub-ap >/dev/null && echo "IoTHub Wi-Fi is up (Pi = 10.42.0.1)"
else
  echo "Ethernet not connected - the AP will start automatically when you plug it in"
fi

cat <<EOF

Done.
  - Add this to iot_node/secrets.h so the nodes can join it:
       #define AP_PASS "$PASS"
   then rebuild and update the nodes.
 - Status:   nmcli -t -f NAME,DEVICE con show --active
 - Disable:  sudo rm /etc/NetworkManager/dispatcher.d/90-iothub-ap && sudo nmcli con down iothub-ap
EOF
