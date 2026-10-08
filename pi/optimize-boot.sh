#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }

echo "== Current boot time =="
systemd-analyze || true
echo; echo "Slowest units:"; systemd-analyze blame 2>/dev/null | head -8 || true
echo

sudo systemctl disable NetworkManager-wait-online.service 2>/dev/null && echo "- no longer waiting for network at boot" || true
for u in iothub iothub-firmware; do
  f=/etc/systemd/system/$u.service
  [[ -f $f ]] && sudo sed -i 's/^After=network-online.target/After=network.target/; /^Wants=network-online.target/d' "$f"
done

if [[ -d /etc/cloud ]]; then
  sudo touch /etc/cloud/cloud-init.disabled && echo "- cloud-init disabled (first-boot setup already done)"
fi

for s in ModemManager bluetooth hciuart triggerhappy; do
  if systemctl list-unit-files "$s.service" >/dev/null 2>&1 && systemctl is-enabled "$s" >/dev/null 2>&1; then
    sudo systemctl disable --now "$s" >/dev/null 2>&1 && echo "- disabled $s"
  fi
done

CFG=/boot/firmware/config.txt
[[ -f $CFG ]] || CFG=/boot/config.txt
add_cfg() { grep -q "^$1" "$CFG" || { echo "$1" | sudo tee -a "$CFG" >/dev/null; echo "- config.txt: $1"; }; }
grep -q "^# iothub boot tweaks" "$CFG" || echo -e "\n# iothub boot tweaks" | sudo tee -a "$CFG" >/dev/null
add_cfg "disable_splash=1"
add_cfg "boot_delay=0"
add_cfg "initial_turbo=30"
add_cfg "dtoverlay=disable-bt"

for t in apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer; do
  if systemctl is-enabled "$t" >/dev/null 2>&1; then
    sudo systemctl disable --now "$t" >/dev/null 2>&1 && echo "- disabled $t"
  fi
done
for s in e2scrub_reap rpi-eeprom-update keyboard-setup; do
  if systemctl is-enabled "$s" >/dev/null 2>&1; then
    sudo systemctl disable "$s" >/dev/null 2>&1 && echo "- disabled $s"
  fi
done

if systemctl is-enabled dphys-swapfile >/dev/null 2>&1; then
  sudo dphys-swapfile swapoff 2>/dev/null || true
  sudo systemctl disable dphys-swapfile >/dev/null 2>&1 && echo "- SD-card swap file off"
fi

set_cfg() {
  local key=${1%%=*}
  if grep -q "^$key=" "$CFG"; then
    grep -qx "$1" "$CFG" || { sudo sed -i "s/^$key=.*/$1/" "$CFG"; echo "- config.txt: $1"; }
  else
    add_cfg "$1"
  fi
}
set_cfg "camera_auto_detect=0"
set_cfg "display_auto_detect=0"
set_cfg "dtparam=audio=off"

sudo systemctl daemon-reload
echo
echo "Boot path (what the hub waited for last boot):"
systemd-analyze critical-chain iothub.service 2>/dev/null | tail -n +2 | head -12 || true
echo
echo "Done. Reboot to apply:  sudo reboot"
echo "Afterwards compare with: systemd-analyze   and   systemd-analyze blame | head"
echo
echo "To undo: sudo systemctl enable NetworkManager-wait-online ModemManager bluetooth dphys-swapfile"
echo "         apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer;"
echo "         sudo rm /etc/cloud/cloud-init.disabled; remove the 'iothub boot tweaks' lines from $CFG"
