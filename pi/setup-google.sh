#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }
CONF="$HOME/.config/iothub/google.env"
mkdir -p "$(dirname "$CONF")"
[[ -f $CONF ]] && source "$CONF"
grep -qs '^DASH_PASSWORD=..' "$HOME/.config/iothub/dashboard.env" || { echo "Set a dashboard admin password first:  iothub password"; exit 1; }

url=$(tailscale funnel status 2>/dev/null | grep -oE 'https://[^ :]+' | head -1 || true)
[[ -n $url ]] || { echo "No public HTTPS link found. Turn on Tailscale Funnel first:  sudo tailscale funnel --bg 8080"; exit 1; }
url=${url%/}

GH_CLIENT_ID=${GH_CLIENT_ID:-iot-pi32-$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')}
GH_CLIENT_SECRET=${GH_CLIENT_SECRET:-$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)}

cat <<EOF

1. Open https://console.home.google.com and sign in with the Google account your
   Lenovo clock / Google Home app uses.
2. Create a project (any name), then add a "Cloud-to-cloud" integration.
3. Fill in these fields:

   Integration name     IoT_Pi32
   Device type          Sensor
   OAuth Client ID      $GH_CLIENT_ID
   OAuth Client secret  $GH_CLIENT_SECRET
   Authorization URL    $url/google/authorize
   Token URL            $url/google/token
   Cloud fulfillment URL  $url/google/fulfillment

   Leave scopes empty. Save.
EOF
read -rp "4. Paste the project ID shown in the console (Enter to skip): " pid
GH_PROJECT_ID=${pid:-${GH_PROJECT_ID:-}}

printf 'GH_CLIENT_ID=%q\nGH_CLIENT_SECRET=%q\nGH_PROJECT_ID=%q\nGH_INCLUDE_PI=%q\nGH_LED=%q\n' \
  "$GH_CLIENT_ID" "$GH_CLIENT_SECRET" "$GH_PROJECT_ID" "${GH_INCLUDE_PI:-yes}" "${GH_LED:-yes}" > "$CONF"

SA="$HOME/.config/iothub/google-service-account.json"
if [[ -f $SA ]]; then
  echo "Live updates: service account key found."
else
  cat <<EOF2

Optional - live updates (values refresh in the Google Home app by themselves, new nodes
appear without "sync my devices"):
  a. https://console.cloud.google.com/apis/library/homegraph.googleapis.com?project=${GH_PROJECT_ID:-YOUR-PROJECT}
     -> Enable
  b. https://console.cloud.google.com/iam-admin/serviceaccounts?project=${GH_PROJECT_ID:-YOUR-PROJECT}
     -> Create service account (any name, no roles needed) -> open it -> Keys -> Add key -> JSON
  c. From the laptop:  scp ~/Downloads/<downloaded-key>.json pi:~/.config/iothub/google-service-account.json
  d. Run this script again.
EOF2
fi
sudo apt-get install -y -qq python3-cryptography >/dev/null 2>&1 || true
[[ -f $SA ]] && chmod 600 "$SA"
chmod 600 "$CONF"
sudo systemctl restart iothub
sleep 3
code=$(curl -s -o /dev/null -w '%{http_code}' "$url/google/authorize?client_id=$GH_CLIENT_ID&redirect_uri=https://oauth-redirect.googleusercontent.com/r/${GH_PROJECT_ID:-test}&state=x&response_type=code" || true)
[[ $code == 200 ]] && echo "Hub check: the link page answers over the internet." || echo "Hub check failed (HTTP $code) - is the dashboard running? iothub status"

cat <<EOF

5. In the console, open "Test" for the integration (test mode is enough for your own account).
6. On your phone: Google Home app -> + -> Device -> Works with Google Home ->
   search "IoT_Pi32" (shown with [test]) -> sign in with the hub admin password -> Allow.
7. Put node 1 in a room, then try:
     "Hey Google, what's the temperature of node 1?"   /  "...humidity of node 1?"
     "Hey Google, what's the temperature of node 1 high?"  (today's highest; "node 1 low" = lowest)
     "Hey Google, turn off node 1 light"  /  "set node 1 light to 20%"
     "Hey Google, activate find node 1"       (rainbow LED for 10 s)
     "Hey Google, activate restart node 1"
     "Hey Google, what's the temperature of the hub?"
     "Hey Google, activate day summary"       (spoken briefing, see setup-speaker.sh)
     "Hey Google, activate find all nodes"  /  "activate send hub report"

Shorter phrases: make routines (Automations -> +, starter "When I say..."), e.g.
  "find node 1" -> activate find node 1 | "restart node 1" -> activate restart node 1
  "turn off node 1" -> turn off node 1 light

Reminders and spoken summaries: make a routine in the Google Home app (Automations -> +).
  Starter: a time (e.g. 8:00) or a phrase (e.g. "hub status").
  Actions: "Try adding your own" -> type each question, e.g.
           "what's the temperature of node 1", "what's the humidity of node 1",
           "what's the temperature of node 1 high", "what's the temperature of node 1 low".
  Play it on: your Lenovo clock.

Without live updates, after adding a node say "Hey Google, sync my devices".
Options in ~/.config/iothub/google.env: GH_INCLUDE_PI (hub sensor), GH_LED (LED control), GH_STATS (high/low).
EOF
