#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }

cur=$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)
read -rp "Time zone [$cur]: " TZ_NAME
TZ_NAME=${TZ_NAME:-$cur}
sudo timedatectl set-timezone "$TZ_NAME" && echo "- time zone: $TZ_NAME"

sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/iothub-ram.conf >/dev/null <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=40M
EOF
echo "- system logs kept in RAM (max 40 MB)"

if ! findmnt -no OPTIONS / | grep -q noatime; then
  sudo sed -i -E '/[[:space:]]\/[[:space:]]/ s/(defaults)([,[:space:]])/\1,noatime\2/' /etc/fstab
  echo "- root filesystem: noatime added to /etc/fstab"
else
  echo "- root filesystem already uses noatime"
fi

if systemctl list-unit-files dphys-swapfile.service >/dev/null 2>&1 && systemctl is-enabled dphys-swapfile >/dev/null 2>&1; then
  sudo dphys-swapfile swapoff || true
  sudo systemctl disable --now dphys-swapfile >/dev/null 2>&1
  echo "- swap file on SD card disabled"
fi

if [[ -d /etc/mosquitto/conf.d ]] && ! grep -rqs autosave_interval /etc/mosquitto/; then
  echo "autosave_interval 3600" | sudo tee /etc/mosquitto/conf.d/iothub-autosave.conf >/dev/null
  echo "- Mosquitto autosave: hourly"
fi

echo
echo "Done. Reboot to apply everything:  sudo reboot"
echo "Hub database: written every minute (change FLUSH_MINUTES in ~/.config/iothub/storage.env)."
