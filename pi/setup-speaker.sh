#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not with sudo"; exit 1; }
CONF="$HOME/.config/iothub/speaker.env"
mkdir -p "$(dirname "$CONF")"
[[ -f $CONF ]] && source "$CONF"

echo "Installing text-to-speech and casting (first time only)..."
sudo apt-get install -y -qq python3-pychromecast python3-gtts >/dev/null 2>&1 \
  || pip3 install --break-system-packages -q pychromecast gTTS

echo "Looking for Google speakers on this network (10 s)..."
mapfile -t FOUND < <(python3 - <<'PY'
import pychromecast
casts, browser = pychromecast.get_chromecasts(timeout=10)
browser.stop_discovery()
for c in sorted({c.cast_info.friendly_name for c in casts}):
    print(c)
PY
)
if [[ ${#FOUND[@]} -eq 0 ]]; then
  echo "No speakers found. The speaker and the Pi must be on the same Wi-Fi/router,"
  echo "and the network must allow devices to see each other (many college networks don't)."
  echo "The summary still works on the dashboard (Read aloud), by email, and on your phone."
  exit 1
fi
echo
for i in "${!FOUND[@]}"; do echo "  $((i + 1))) ${FOUND[$i]}"; done
read -rp "Which one should speak? [1]: " pick
pick=${pick:-1}
[[ $pick =~ ^[0-9]+$ && $pick -ge 1 && $pick -le ${#FOUND[@]} ]] || { echo "pick a number from the list"; exit 1; }
SPEAKER_NAME=${FOUND[$((pick - 1))]}

ask() { local v; read -rp "$1 [${2}]: " v; echo "${v:-$2}"; }
SUMMARY_TIME=$(ask "Speak the day summary automatically every day at (24 h like 21:00, '-' for never)" "${SUMMARY_TIME:-21:00}")
[[ $SUMMARY_TIME == "-" ]] && SUMMARY_TIME=""
[[ -z $SUMMARY_TIME || $SUMMARY_TIME =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "time must look like 21:00"; exit 1; }
cat <<'EOF'

Voice (British female):
  1) Jenny  - warm, natural (recommended)
  2) Cori   - clear, brighter
  3) Alba   - soft Scottish
  4) Southern English
  5) Google's online voice (no download, more robotic)
EOF
v=$(ask "Pick a voice" "1")
case $v in
  2) PIPER_VOICE=en_GB-cori-medium;;
  3) PIPER_VOICE=en_GB-alba-medium;;
  4) PIPER_VOICE=en_GB-southern_english_female-low;;
  *) PIPER_VOICE=en_GB-jenny_dioco-medium;;
esac
SPEAK_ENGINE=piper
SPEAK_TLD=co.uk
if [[ $v == 5 ]]; then
  SPEAK_ENGINE=gtts
else
  echo "Installing the voice (about 70 MB, first time only)..."
  PIPER_DIR="$HOME/.local/share/piper"
  mkdir -p "$PIPER_DIR"
  if ! python3 -c 'import piper' 2>/dev/null; then
    pip3 install --break-system-packages -q piper-tts || { echo "Piper could not be installed - using Google's voice instead"; SPEAK_ENGINE=gtts; }
  fi
  if [[ $SPEAK_ENGINE == piper && ! -f $PIPER_DIR/$PIPER_VOICE.onnx ]]; then
    python3 -m piper.download_voices "$PIPER_VOICE" --download-dir "$PIPER_DIR" \
      || { echo "Voice download failed - using Google's voice instead"; SPEAK_ENGINE=gtts; }
  fi
fi
a=$(ask "Also send the summary to your phone (ntfy/Telegram)? y/n" "${SUMMARY_PUSH:-yes}")
[[ $a =~ ^[Yy] ]] && SUMMARY_PUSH=yes || SUMMARY_PUSH=no

printf 'SPEAKER_NAME=%q\nSUMMARY_TIME=%q\nSPEAK_ENGINE=%q\nPIPER_VOICE=%q\nSPEAK_TLD=%q\nSPEAK_LANG=%q\nSUMMARY_PUSH=%q\n' \
  "$SPEAKER_NAME" "$SUMMARY_TIME" "$SPEAK_ENGINE" "${PIPER_VOICE:-en_GB-jenny_dioco-medium}" "$SPEAK_TLD" "${SPEAK_LANG:-en}" "$SUMMARY_PUSH" > "$CONF"
sudo systemctl restart iothub
sleep 4
printf '{"text": "Hello. This is your IoT hub. I will read your daily summaries on this speaker."}' > "$HOME/iothub-data/speak.request"
echo
echo "Saved. '$SPEAKER_NAME' should say hello within a few seconds."
echo "  Day summary now:      iothub speak            (yesterday: iothub speak yesterday)"
echo "  Read it as text:      iothub summary"
echo "  By voice:             \"Hey Google, activate day summary\"  (after \"Hey Google, sync my devices\")"
[[ -n $SUMMARY_TIME ]] && echo "  Automatically:        every day at $SUMMARY_TIME"
echo "Nothing heard? Check: iothub logs   (look for 'speaking failed')"
