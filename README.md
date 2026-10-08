<div align="center">

# IoT Hub

**Temperature and humidity monitoring with ESP32-S3 sensor nodes and a Raspberry Pi hub.**
Live dashboard from anywhere, phone alerts, email reports, cloud upload and over-the-air updates,
built to keep every reading through Wi-Fi drops and power cuts.

![Raspberry Pi 4](https://img.shields.io/badge/hub-Raspberry%20Pi%204-c51a4a?logo=raspberrypi&logoColor=white)
![ESP32-S3](https://img.shields.io/badge/nodes-ESP32--S3-e7352c?logo=espressif&logoColor=white)
![Python](https://img.shields.io/badge/hub-Python%203-3776ab?logo=python&logoColor=white)
![Arduino](https://img.shields.io/badge/firmware-Arduino%20core%203-00979d?logo=arduino&logoColor=white)
![MQTT](https://img.shields.io/badge/protocol-MQTT-660066?logo=mqtt&logoColor=white)

<img src="docs/images/overview.png" alt="Dashboard overview: live readings from three nodes and the Pi, 24-hour temperature and humidity charts, recent events" width="900">

</div>

---

## Contents

- [Highlights](#highlights)
- [Screenshots](#screenshots)
- [How it works](#how-it-works)
- [Hardware](#hardware)
- [Getting started](#getting-started)
- [Everyday use](#everyday-use)
- [Configuration](#configuration)
- [Reference](#reference)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

## Highlights

| | |
| --- | --- |
| **No lost readings** | Nodes store up to 8000 readings in flash while the hub is away, then send them back. After every reconnect they also resend their last ~6 minutes, so readings that seemed sent just before a power cut aren't lost. The hub keeps only the ones it doesn't have. |
| **Dashboard anywhere** | Live readings, any-day history, daily min/avg/max, alerts, interruptions and hub health. Shared publicly over HTTPS with Tailscale Funnel, guest view by default, admin sign-in for controls. Installs as an app on phones and desktops. |
| **Knows what went wrong** | Every Pi power cut, reboot and crash is recorded with downtime, boot time and the readings lost per node. Node restarts include the reason (power on, brownout, crash, update). Every gap in the data gets a likely cause. |
| **Alerts and reports** | Push notifications with ntfy (free, no account) and/or Telegram. Email reports with charts to any address, daily or on demand. |
| **Updates over Wi-Fi** | `./deploy.sh ota node1` builds on the laptop, hands the image to the Pi and the node pulls it from there. If the new firmware can't reach the hub it rolls back by itself. |
| **Kind to the SD card** | Readings are batched in RAM and written once a minute, logs live in RAM, raw data is kept 30 days, daily summaries forever. |
| **Works without a router** | Plug an Ethernet cable into the Pi and it becomes the access point for the nodes. Nodes find the hub on their own: gateway, mDNS, then last known address. |
| **Readable LED** | Each node's RGB LED shows its state with smooth animations, plus a rainbow "find me" mode. |

## Screenshots

<table>
<tr>
<td width="50%"><img src="docs/images/history.png" alt="History: temperature and humidity side by side, daily summary bars"><br><sub><b>History</b> – any range or day, temperature and humidity side by side, daily min/avg/max. Small gaps are drawn as dashed estimates.</sub></td>
<td width="50%"><img src="docs/images/interruptions.png" alt="Interruptions: power cuts, boot time, readings lost, data completeness"><br><sub><b>Interruptions</b> – power cuts, boot times, readings lost and restored, data completeness per node, node restarts.</sub></td>
</tr>
<tr>
<td><img src="docs/images/hub.png" alt="Hub page: CPU, memory, SD card, network, broker, integrations"><br><sub><b>Hub</b> – Pi health, connections, email reports, alert rules, storage, background jobs.</sub></td>
<td><img src="docs/images/overview-dark.png" alt="Overview in dark mode"><br><sub><b>Dark mode</b> – follows the system setting.</sub></td>
</tr>
<tr>
<td><img src="docs/images/nodes.png" alt="Node cards"><br><sub><b>Nodes</b> – readings first, technical details folded away; admins get brightness, find, interval and restart.</sub></td>
<td align="center"><img src="docs/images/phone.png" alt="Dashboard on a phone" width="240"><br><sub><b>Phone</b> – bottom tab bar, installable as an app.</sub></td>
</tr>
</table>

<sub>Screenshots use demo data.</sub>

## How it works

```mermaid
flowchart LR
    subgraph Nodes["ESP32-S3 nodes"]
        S[AM2305B / DS18B20] --> F[firmware]
        F <--> B[(flash buffer)]
        F --> L[status LED]
    end
    subgraph Pi["Raspberry Pi 4"]
        M[Mosquitto] --> H[hub service]
        H --> D[(SQLite)]
        H --> W[dashboard :8080]
        FW[firmware server :8000]
    end
    F -- MQTT --> M
    FW -. OTA .-> F
    W -- Tailscale / Funnel --> U[browser / app]
    H --> A[Adafruit IO]
    H --> N[ntfy / Telegram]
    H --> E[email reports]
    LT[laptop] -- deploy.sh ota --> FW
```

**Readings.** Each node publishes temperature and humidity every 10 s (adjustable). The hub holds them
in memory and writes them to SQLite in one transaction a minute. A daily min/avg/max table is updated
with each write and kept forever, while raw readings are kept for 30 days.

**When the hub is unreachable** the node writes readings to a ring buffer in flash, dated by NTP or
the hub's own clock. When the hub is back, the node sends them in batches and the charts fill in.

**After a sudden Pi power cut** the node's TCP connection still looks open for a while, and the hub
loses whatever it hadn't written yet. To cover both, every node resends its last few minutes of
readings after each reconnect, and the hub drops duplicates. Ten minutes after start-up the hub
counts what is still missing and records it on the Interruptions page.

**No clock battery.** The Pi doesn't know the time until NTP answers. Readings taken before that are
held with a monotonic timestamp and dated once the clock is confirmed, and nodes are only sent the
hub's time after that.

**Updates** never come from the internet directly:

```mermaid
sequenceDiagram
    participant L as Laptop
    participant P as Raspberry Pi
    participant N as Node
    L->>L: arduino-cli compile
    L->>P: scp firmware.bin
    L->>P: ota-push node1
    P->>N: MQTT cmd "ota http://pi-address:8000/fw.bin"
    N->>N: accept only URLs on the hub's own address
    N->>P: HTTP GET firmware
    N->>N: write spare slot, reboot
    N->>P: MQTT "booted fw=…"
    Note over N: no hub within 3 min → roll back
```

## Hardware

| Part | Notes |
| --- | --- |
| Raspberry Pi 4 | Raspberry Pi OS Lite 64-bit, a proper 5.1 V 3 A supply, preferably a high-endurance SD card |
| ESP32-S3 dev board | tested on ESP32-S3-DevKitM-1; onboard RGB LED on GPIO 48 |
| AM2305B sensor | temperature and humidity; or a DS18B20 for temperature only |
| 4.7 kΩ resistor | pull-up for the sensor data line |

**Wiring (AM2305B or DS18B20)**

| Sensor | ESP32-S3 |
| --- | --- |
| VDD (red) | 3V3 |
| GND (black) | GND |
| DATA (yellow) | GPIO 1, plus 4.7 kΩ to 3V3 |

Avoid GPIO 0, which is the BOOT strapping pin. Give each node its own USB supply so it keeps
recording when the Pi loses power.

## Getting started

### 1. Prepare the Pi

Flash Raspberry Pi OS Lite with SSH enabled, then:

```bash
sudo apt update && sudo apt install -y mosquitto mosquitto-clients
sudo mosquitto_passwd -c /etc/mosquitto/passwd esp
printf 'listener 1883\nallow_anonymous false\npassword_file /etc/mosquitto/passwd\n' \
  | sudo tee /etc/mosquitto/conf.d/iothub.conf
sudo systemctl restart mosquitto
```

For access from anywhere, install [Tailscale](https://tailscale.com):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
sudo tailscale funnel --bg 8080      # optional: public HTTPS link to the dashboard
```

### 2. Install the hub

From your laptop (replace `<pi>` with the Pi's SSH host):

```bash
scp -r pi <pi>:~/iothub-ota
ssh -t <pi> bash iothub-ota/setup-ota.sh          # MQTT login, firmware server, ota-push
ssh -t <pi> bash iothub-ota/setup-dashboard.sh    # hub service, dashboard, iothub command
```

The dashboard is now at `http://<pi>:8080`.

### 3. Optional extras

| Script | Adds |
| --- | --- |
| `setup-alerts.sh` | ntfy / Telegram notifications and temperature / humidity limits |
| `setup-email.sh` | email reports (sent from a Gmail account with an app password) |
| `setup-adafruit.sh` | upload to Adafruit IO (free plan limits respected) |
| `setup-network.sh` | Ethernet mode: the Pi runs the `IoTHub` Wi-Fi for the nodes |
| `protect-sd.sh` | fewer SD card writes, time zone (reboot after) |
| `optimize-boot.sh` | faster boot for a headless Pi (reboot after) |

Run them on the Pi with `bash ~/iothub-ota/<script>`. When you copy an updated `pi` folder later,
`scp` places it at `~/iothub-ota/pi/`, so run the scripts from there.

### 4. Flash the first node

On the laptop, with [arduino-cli](https://arduino.github.io/arduino-cli/) installed and your user
allowed to use serial ports:

```bash
./deploy.sh setup                                   # ESP32 core and libraries, once
cp iot_node/secrets.example.h iot_node/secrets.h    # Wi-Fi, MQTT password, LED pin
./deploy.sh usb                                     # build, flash over USB, open the monitor
```

If the board doesn't show up, hold **BOOT** while plugging it in, and use the port labelled USB.
Then name it:

```bash
ssh <pi> iothub rename esp-a1b2c3 node1
```

`deploy.sh` reaches the Pi through the SSH host `pi`; set `PI_HOST` to use another.

## Everyday use

### Update firmware over Wi-Fi

```bash
./deploy.sh ota node1      # or: ./deploy.sh ota all
```

It ends with `booted fw=<version>`. `./deploy.sh usb` always works as a fallback.

### The `iothub` command

Run on the Pi (or `ssh <pi> iothub …`):

| Command | |
| --- | --- |
| `iothub status` | services, links, nodes online, open problems |
| `iothub nodes` | every node: readings, sensor, firmware, signal |
| `iothub watch [node]` | live MQTT stream |
| `iothub logs` | live hub log |
| `iothub interruptions [days]` | power cuts, boot times, readings lost, node restarts |
| `iothub adafruit` | cloud upload status |
| `iothub find <node>` | rainbow LED for 10 s |
| `iothub interval <node\|all> <s>` | report interval |
| `iothub brightness <node\|all> <0-255>` | LED brightness |
| `iothub reboot-node <node\|all>` | restart nodes |
| `iothub rename <node> <name>` | rename a node |
| `iothub ota <node\|all> <file.bin>` | push a firmware image |
| `iothub report [hours] [emails]` | email a report now |
| `iothub export [days]` | readings as CSV |
| `iothub backup` | copy of the database |
| `iothub reset-readings [--node N] [--events]` | delete readings (backs up first) |
| `iothub clear-node-buffers` | nodes drop stored offline readings |
| `iothub reset-all` | fresh start (backs up first) |
| `iothub password` | change the dashboard admin password |
| `iothub test-alert` | send a test notification |
| `iothub restart` | restart the hub service |

### What the LED means

| LED | State |
| --- | --- |
| 🟢 green, slow breathing, white flash per reading | online and healthy |
| 🔵 blue pulse | looking for Wi-Fi |
| 🟠 amber heartbeat | hub unreachable, storing readings |
| 🩵 cyan shimmer | sending stored readings |
| 🔴 red heartbeat | sensor not reading |
| 🟣 purple | firmware update |
| 🌈 rainbow | "find node" |

### Install the dashboard as an app

Open the HTTPS (Funnel) link. In Chrome, Edge or Android use **Install app**; on iPhone use
Safari → Share → **Add to Home Screen**.

## Configuration

**Node** – `iot_node/secrets.h` (copied from `secrets.example.h`, ignored by git):

| Setting | |
| --- | --- |
| `WIFI_SSID`, `WIFI_PASS` | your 2.4 GHz Wi-Fi |
| `MQTT_USER`, `MQTT_PASS` | the Mosquitto login |
| `AP_PASS` | password of the Pi's `IoTHub` Wi-Fi (Ethernet mode), empty if unused |
| `MQTT_HOST` | optional fixed hub IP, last resort after gateway and mDNS |
| `LED_PIN` | onboard RGB LED (48 on DevKitM-1) |
| `USE_DS18B20`, `SENSOR_PIN` | sensor choice and pin |

**Hub** – files in `~/.config/iothub/` on the Pi, written by the setup scripts:

| File | Contents |
| --- | --- |
| `mqtt.env` | MQTT login |
| `dashboard.env` | admin password, port |
| `alerts.env` | ntfy / Telegram, limits |
| `adafruit.env` | Adafruit IO username and key |
| `email.env` | sender, recipients, daily report time |
| `storage.env` | `FLUSH_MINUTES` (1), `RAW_KEEP_DAYS` (30), `FILL_MAX_MINUTES` (15) |

Apply changes with `iothub restart`.

## Reference

### MQTT topics

| Topic | |
| --- | --- |
| `home/<node>/temp`, `hum` | readings |
| `home/<node>/backlog` | stored readings `{"r": [[epoch, temp, hum], …]}` |
| `home/<node>/status` | `online` / `offline` (retained, last will) |
| `home/<node>/info` | JSON details (retained) |
| `home/<node>/ota/status`, `log` | update progress, messages |
| `home/<node>/cmd`, `home/all/cmd` | `reboot`, `info`, `identify`, `brightness N`, `interval S`, `name NEW`, `clearbuffer`, `ota URL` |
| `home/hub/time` | hub clock for nodes without NTP |

### HTTP API

Everything the dashboard shows is plain JSON. `POST` endpoints need an admin session.

| Endpoint | |
| --- | --- |
| `GET /api/nodes` | nodes, alerts, integrations, storage |
| `GET /api/history?hours=24&metric=temp` | chart series (`start`/`end` also accepted) |
| `GET /api/daily?days=30&metric=hum` | daily min/avg/max |
| `GET /api/events` | event log with filters |
| `GET /api/interruptions?days=30` | outages, gaps, node restarts, completeness |
| `GET /api/health`, `/api/config` | service health, settings |
| `GET /api/export.csv`, `/api/daily.csv`, `/api/events.csv`, `/api/interruptions.csv` | downloads (`export.csv?fill=1` adds estimates) |
| `POST /api/login`, `/api/logout` | admin session |
| `POST /api/nodes/<id>/cmd` | `{"cmd": "identify"}` etc. |
| `POST /api/report/send`, `/api/alerts/test`, `/api/alerts/<key>/ack` | actions |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `No board found` | hold BOOT while plugging in; use the USB (not UART) port; try a data-capable cable |
| USB error `-71` on Linux | same as above: BOOT while plugging in |
| LED amber heartbeat | node has Wi-Fi but no hub: check `iothub status` and the MQTT password |
| LED red heartbeat | sensor wiring or pull-up; data must be on `SENSOR_PIN` |
| Node never joins `IoTHub` | set `AP_PASS` in `secrets.h` to the password used in `setup-network.sh` |
| `ota-push` says node not online | the node must be connected to the Pi's broker; check `iothub nodes` |
| Can't install the dashboard as an app | use the HTTPS Funnel link, not the plain `http://` address |
| Gaps after a power cut | normal if the node lost power too; give nodes their own supply |
| Under-voltage warnings | use a 5.1 V 3 A supply for the Pi |

## Project layout

```
iot_node/
  iot_node.ino            node firmware
  secrets.example.h       settings template (copy to secrets.h)
deploy.sh                 laptop: setup, build, usb, ota, monitor
pi/
  setup-*.sh              one-time setup scripts (see Getting started)
  protect-sd.sh           SD card protection
  optimize-boot.sh        faster boot
  ota-push                push firmware to nodes
  iothub                  management command
  dashboard/
    iothub.py             hub service: MQTT, storage, alerts, reports, uploads, web API
    static/               dashboard web app, manifest, service worker, icons
docs/images/              screenshots
```
