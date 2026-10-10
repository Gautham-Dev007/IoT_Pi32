#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

FQBN="esp32:esp32:esp32s3:CDCOnBoot=cdc,FlashSize=4M,PartitionScheme=min_spiffs"
SKETCH=iot_node
PI=${PI_HOST:-pi}
PORT=${2:-}

find_port() {
  [[ -n $PORT ]] && return
  PORT=$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -n1 || true)
  [[ -n $PORT ]] || { echo "No board found. Plug it in; if needed hold BOOT, tap RST, release BOOT."; exit 1; }
}

build() {
  [[ -f $SKETCH/secrets.h ]] || { echo "Create $SKETCH/secrets.h first:  cp $SKETCH/secrets.example.h $SKETCH/secrets.h  and edit it"; exit 1; }
  arduino-cli compile --fqbn "$FQBN" --output-dir build "$SKETCH"
}

case ${1:-} in
  setup)
    arduino-cli config init >/dev/null 2>&1 || true
    arduino-cli config add board_manager.additional_urls \
      https://espressif.github.io/arduino-esp32/package_esp32_index.json 2>/dev/null || true
    arduino-cli core update-index
    arduino-cli core install esp32:esp32
    arduino-cli lib install PubSubClient DHTNEW OneWire DallasTemperature
    ;;
  build) build ;;
  usb)
    find_port; build
    arduino-cli upload -p "$PORT" --fqbn "$FQBN" --input-dir build "$SKETCH"
    sleep 2; find_port
    arduino-cli monitor -p "$PORT" -c baudrate=115200
    ;;
  ota)
    NODE=${2:?usage: ./deploy.sh ota <node-id|all>}
    build
    scp "build/$SKETCH.ino.bin" "$PI:/tmp/$SKETCH.bin"
    ssh -t "$PI" "ota-push $NODE /tmp/$SKETCH.bin"
    ;;
  monitor) find_port; arduino-cli monitor -p "$PORT" -c baudrate=115200 ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
