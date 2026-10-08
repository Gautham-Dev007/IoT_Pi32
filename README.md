# IoT Hub

A Raspberry Pi 4 hub that collects temperature and humidity from ESP32-S3 sensor nodes, stores it,
shows it on a live dashboard reachable from anywhere, uploads it to Adafruit IO, and sends alerts
and email reports. Nodes are updated over Wi-Fi through the Pi, and keep recording while the Pi is
down.

```
 ESP32-S3 nodes ──MQTT──►  Raspberry Pi 4  ──►  Dashboard (Tailscale / Funnel, installable app)
 AM2305B / DS18B20          Mosquitto          ──►  Adafruit IO
 status LED                 hub service        ──►  ntfy / Telegram alerts, email reports
 flash buffer               SQLite             ◄──  OTA firmware (laptop → Pi → node)
```

## Features

**Nodes (ESP32-S3)**
- AM2305B (temperature + humidity) or DS18B20 sensor
- Store and forward: up to 8000 readings kept in flash while the hub is unreachable, sent when it
  returns; the last few minutes are always resent after a reconnect, so a sudden power cut on the
  Pi loses nothing
- Finds the hub automatically: the Pi's own Wi-Fi (Ethernet mode), mDNS `iothub.local`, last known IP
- Over-the-air updates pulled from the Pi only, with automatic rollback if the new firmware can't
  reach the hub
- Animated status LED, brightness control, "find node" rainbow
- Reports why it restarted (power on, brownout, crash, update)

**Hub (Raspberry Pi)**
- MQTT (Mosquitto) → SQLite, written in batches to spare the SD card; daily min/avg/max kept forever
- Dashboard: overview, nodes, history (any day, both metrics side by side, estimated small gaps),
  alerts with categories and acknowledge, interruptions (power cuts, boot times, data lost per
  node, gaps with causes), hub health; guest view by default, admin sign-in for controls;
  installable as an app
- Alerts via ntfy and/or Telegram: Pi power loss, node offline, sensor not reading, limits,
  overheating, under-voltage, disk full, broker down
- Email reports with charts to any address (Gmail app password on the sending side only)
- Adafruit IO upload with per-feed error handling and free-plan rate limiting
- Ethernet mode: the Pi becomes an access point for the nodes when its Ethernet cable is plugged in
- `iothub` command for everything over SSH

## Hardware

| Part | Notes |
| --- | --- |
| Raspberry Pi 4 | Raspberry Pi OS Lite (64-bit), 5.1 V 3 A supply |
| ESP32-S3-DevKitM-1 (or similar) | onboard RGB LED on GPIO 48 |
| AM2305B | VDD → 3V3, GND → GND, DATA → GPIO 1, 4.7 kΩ pull-up DATA → 3V3 |
| DS18B20 (alternative) | same wiring; add `#define USE_DS18B20` to `secrets.h` |

Avoid GPIO 0 for the sensor: it is a boot strapping pin.

## Repository layout

```
iot_node/iot_node.ino          node firmware
iot_node/secrets.example.h     copy to secrets.h and fill in (git-ignored)
deploy.sh                      laptop: build, USB flash, OTA flash, serial monitor
pi/setup-ota.sh                Pi: MQTT login, firmware server, ota-push
pi/setup-dashboard.sh          Pi: hub service, dashboard, iothub command
pi/setup-alerts.sh             Pi: ntfy / Telegram and limits
pi/setup-adafruit.sh           Pi: Adafruit IO upload
pi/setup-email.sh              Pi: email reports
pi/setup-network.sh            Pi: Ethernet mode access point
pi/protect-sd.sh               Pi: fewer SD card writes, time zone
pi/optimize-boot.sh            Pi: faster boot
pi/ota-push                    Pi: push a firmware image to nodes
pi/iothub                      Pi: management command
pi/dashboard/                  hub service (Python) and web app
```

## Setup

### 1. Raspberry Pi

Flash Raspberry Pi OS Lite with SSH enabled, then on the Pi:

```
sudo apt update && sudo apt install -y mosquitto mosquitto-clients
sudo mosquitto_passwd -c /etc/mosquitto/passwd esp
printf 'listener 1883\nallow_anonymous false\npassword_file /etc/mosquitto/passwd\n' | sudo tee /etc/mosquitto/conf.d/iothub.conf
sudo systemctl restart mosquitto
```

Copy the `pi` folder over and run the setup scripts (as your normal user, not root):

```
scp -r pi <pi-host>:~/iothub-ota
ssh -t <pi-host> bash iothub-ota/setup-ota.sh
ssh -t <pi-host> bash iothub-ota/setup-dashboard.sh
```

Optional, any order:

```
bash ~/iothub-ota/setup-alerts.sh      # phone notifications
bash ~/iothub-ota/setup-adafruit.sh    # cloud upload
bash ~/iothub-ota/setup-email.sh       # email reports
bash ~/iothub-ota/setup-network.sh     # Ethernet mode access point
bash ~/iothub-ota/protect-sd.sh        # then reboot
bash ~/iothub-ota/optimize-boot.sh     # then reboot
```

When updating later, `scp` puts the folder at `~/iothub-ota/pi/` if `~/iothub-ota` already exists;
run the scripts from there.

**Remote access** with [Tailscale](https://tailscale.com): `curl -fsSL https://tailscale.com/install.sh | sh`,
`sudo tailscale up --ssh`. To make the dashboard public over HTTPS (needed to install it as an app):
`sudo tailscale funnel --bg 8080`.

### 2. Laptop

Needs [arduino-cli](https://arduino.github.io/arduino-cli/) and serial port access.

```
./deploy.sh setup                                   # ESP32 core + libraries
cp iot_node/secrets.example.h iot_node/secrets.h    # then edit it
```

`deploy.sh` reaches the Pi through the SSH host `pi` (set `PI_HOST` to use another).

### 3. Nodes

First flash over USB (hold BOOT while plugging in if the port doesn't appear):

```
./deploy.sh usb
```

Give it a name, then every later update goes over Wi-Fi through the Pi:

```
ssh <pi-host> iothub rename esp-a1b2c3 node1
./deploy.sh ota node1        # or: ./deploy.sh ota all
```

An update ends with `booted fw=<version>`. If the new firmware can't reach the hub within
3 minutes, the node rolls back on its own. `./deploy.sh usb` always works as a fallback.

## Using it

### `iothub` command (on the Pi)

| Command | What it does |
| --- | --- |
| `iothub status` | services, dashboard links, nodes online, open problems |
| `iothub nodes` | every node with readings, sensor, firmware, signal |
| `iothub watch [node]` | live MQTT stream |
| `iothub logs` | live hub log |
| `iothub interruptions [days]` | power cuts, boot times, readings lost, node restarts |
| `iothub adafruit` | upload status |
| `iothub find <node>` | rainbow LED for 10 s |
| `iothub interval <node\|all> <s>` | report interval |
| `iothub brightness <node\|all> <0-255>` | LED brightness |
| `iothub reboot-node <node\|all>` | restart a node |
| `iothub rename <node> <name>` | rename a node |
| `iothub ota <node\|all> <file.bin>` | push firmware |
| `iothub report [hours] [emails]` | email a report now |
| `iothub export [days]` | CSV of readings |
| `iothub backup` | copy of the database |
| `iothub reset-readings [--node N] [--events]` | delete readings (backs up first) |
| `iothub clear-node-buffers` | nodes drop stored offline readings |
| `iothub reset-all` | fresh start (backs up first) |
| `iothub password` | change the dashboard admin password |
| `iothub test-alert` | send a test notification |
| `iothub restart` | restart the hub service |

### Status LED

| LED | Meaning |
| --- | --- |
| Green, slow breathing + white flash | online, reading sent |
| Blue pulse | looking for Wi-Fi |
| Amber heartbeat | hub unreachable, storing readings |
| Cyan shimmer | sending stored readings |
| Red heartbeat | sensor not reading |
| Purple | firmware update |
| Rainbow | find node |

### MQTT topics

```
home/<node>/temp, hum            readings
home/<node>/backlog              stored readings  {"r": [[epoch, temp, hum], ...]}
home/<node>/status               online / offline (retained, last will)
home/<node>/info                 JSON details (retained)
home/<node>/ota/status, log      update progress, messages
home/<node>/cmd, home/all/cmd    reboot | info | identify | brightness N | interval S |
                                 name NEW | clearbuffer | ota URL
home/hub/time                    hub clock for nodes without NTP
```

### Configuration

Settings live in `~/.config/iothub/` on the Pi and are written by the setup scripts:
`mqtt.env`, `dashboard.env`, `alerts.env`, `adafruit.env`, `email.env`, `storage.env`.
`storage.env` options: `FLUSH_MINUTES` (default 1), `RAW_KEEP_DAYS` (30),
`FILL_MAX_MINUTES` (15, longest gap drawn as an estimate). Restart with `iothub restart`.

## Notes

- The ESP32-S3 supports 2.4 GHz Wi-Fi only, and can't log in to captive portals.
- The Pi has no clock battery; readings taken before network time arrives are re-dated once it does.
- If a node shares a power supply with the Pi, a power cut stops both and that time can't be recorded.
  Give nodes their own supply.
