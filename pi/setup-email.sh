#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }
CONF="$HOME/.config/iothub/email.env"
mkdir -p "$(dirname "$CONF")"
[[ -f $CONF ]] && source "$CONF"

echo "Installing the graph library (first time only)..."
sudo apt-get install -y -qq python3-matplotlib >/dev/null

cat <<'EOF'

Sending account (Gmail):
  1. Use a Gmail account for the hub (your own, or a new one).
  2. Turn on 2-Step Verification: myaccount.google.com -> Security.
  3. Create an app password: myaccount.google.com/apppasswords
     Name it "IoT Hub" and copy the 16-letter password it shows.
EOF
ask() { local v; read -rp "$1 [${2}]: " v; echo "${v:-$2}"; }
SMTP_USER=$(ask "Gmail address that sends the emails" "${SMTP_USER:-}")
read -rsp "App password (16 letters, spaces are fine; Enter keeps the saved one): " pw; echo
SMTP_PASS=${pw:-${SMTP_PASS:-}}
[[ $SMTP_USER == *@* && -n $SMTP_PASS ]] || { echo "need a Gmail address and app password"; exit 1; }

echo
REPORT_TO=$(ask "Send to (one or more emails, separated by commas)" "${REPORT_TO:-}")
REPORT_TIME=$(ask "Daily report time, 24 h like 08:00 ('-' for no daily report)" "${REPORT_TIME:-08:00}")
[[ $REPORT_TIME == "-" ]] && REPORT_TIME=""
[[ -z $REPORT_TIME || $REPORT_TIME =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "time must look like 08:00"; exit 1; }
a=$(ask "Also email critical alerts (node offline, sensor not reading, power cut)? y/n" "${REPORT_ALERTS:-yes}")
[[ $a =~ ^[Yy] ]] && REPORT_ALERTS=yes || REPORT_ALERTS=no
SITE_NAME=$(ask "Name shown in the emails" "${SITE_NAME:-IoT Hub}")
url=$(tailscale funnel status 2>/dev/null | grep -oE 'https://[^ ]+' | head -1 || true)
PUBLIC_URL=$(ask "Dashboard link to include ('-' for none)" "${PUBLIC_URL:-$url}")
[[ $PUBLIC_URL == "-" ]] && PUBLIC_URL=""

{
  printf 'SMTP_HOST=%q\nSMTP_PORT=%q\nSMTP_USER=%q\nSMTP_PASS=%q\n' "${SMTP_HOST:-smtp.gmail.com}" "${SMTP_PORT:-587}" "$SMTP_USER" "$SMTP_PASS"
  printf 'REPORT_TO=%q\nREPORT_TIME=%q\nREPORT_ALERTS=%q\nSITE_NAME=%q\nPUBLIC_URL=%q\n' \
    "$REPORT_TO" "$REPORT_TIME" "$REPORT_ALERTS" "$SITE_NAME" "$PUBLIC_URL"
} > "$CONF"
chmod 600 "$CONF"

echo "Checking the Gmail login..."
python3 - "$SMTP_USER" "$SMTP_PASS" <<'PY' || { echo "Gmail refused the login - check the address and app password, then run this again"; exit 1; }
import smtplib, ssl, sys
with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
    s.starttls(context=ssl.create_default_context())
    s.login(sys.argv[1], sys.argv[2].replace(" ", ""))
print("Login OK")
PY

sudo systemctl restart iothub
sleep 3
echo '{"hours": 24}' > "$HOME/iothub-data/report.request"
echo
echo "Saved. A test report is on its way to: $REPORT_TO (within a minute)."
[[ -n $REPORT_TIME ]] && echo "Daily report: every day at $REPORT_TIME."
echo "Send one any time with:  iothub report        (or: iothub report 48 someone@example.com)"
