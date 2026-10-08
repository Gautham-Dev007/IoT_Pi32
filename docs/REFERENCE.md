# IoT_Pi32 reference

Everything you can run, set or call. For an overview and setup steps, see the [README](../README.md).

## Contents

1. [Laptop: `deploy.sh`](#1-laptop-deploysh)
2. [Pi: the `iothub` command](#2-pi-the-iothub-command)
   1. [Monitoring](#21-monitoring)
   2. [Nodes](#22-nodes)
   3. [Reports and data](#23-reports-and-data)
   4. [Maintenance](#24-maintenance)
3. [Node settings](#3-node-settings)
4. [Hub settings](#4-hub-settings)
   1. [Config files](#41-config-files)
   2. [Storage options](#42-storage-options)
5. [Setup scripts](#5-setup-scripts)
6. [MQTT](#6-mqtt)
   1. [Topics](#61-topics)
   2. [Node commands](#62-node-commands)
7. [HTTP API](#7-http-api)
   1. [Read](#71-read)
   2. [Downloads](#72-downloads)
   3. [Actions](#73-actions)

---

## 1. Laptop: `deploy.sh`

Needs [arduino-cli](https://arduino.github.io/arduino-cli/). Reaches the Pi through the SSH host
`pi`; set `PI_HOST` to use another.

| Command | |
| --- | --- |
| `./deploy.sh setup` | install the ESP32 core and libraries (once) |
| `./deploy.sh build` | compile only |
| `./deploy.sh usb [port]` | build, flash over USB, open the serial monitor |
| `./deploy.sh ota <node\|all>` | build, copy to the Pi, update over Wi-Fi |
| `./deploy.sh monitor [port]` | serial monitor |

Build settings: `esp32:esp32:esp32s3`, USB CDC on boot, 4 MB flash, `min_spiffs` partitions.

## 2. Pi: the `iothub` command

Installed by `setup-dashboard.sh`. Run `iothub help` for the same list on the Pi. Commands that
take `<node|all>` act on one node or every node.

### 2.1 Monitoring

| Command | |
| --- | --- |
| `iothub status` | services, dashboard links, nodes online, open problems |
| `iothub nodes` | every node: online, temperature, humidity, sensor, firmware, IP |
| `iothub watch [node]` | live stream of everything the nodes send |
| `iothub logs` | live hub log |
| `iothub interruptions [days]` | power cuts, reboots, boot times, readings lost, node restarts (default 30 days) |
| `iothub adafruit` | what is uploaded to Adafruit IO, and any error |

### 2.2 Nodes

| Command | |
| --- | --- |
| `iothub find <node>` | rainbow LED for 10 s |
| `iothub interval <node\|all> <seconds>` | report interval (2–3600) |
| `iothub brightness <node\|all> <0-255>` | status LED brightness |
| `iothub reboot-node <node\|all>` | restart the ESP32 |
| `iothub rename <node> <name>` | give a node a new name (it restarts) |
| `iothub ota <node\|all> <file.bin>` | push a firmware image |

### 2.3 Reports and data

| Command | |
| --- | --- |
| `iothub report [hours] [email …]` | email a report with charts now (default: last 24 h to the saved recipients) |
| `iothub export [days]` | readings as CSV in `~/iothub-exports` |
| `iothub backup` | copy of the database to `~/iothub-data/backups` |

### 2.4 Maintenance

| Command | |
| --- | --- |
| `iothub reset-readings [--node N] [--events] [--yes]` | delete stored readings (backs up first) |
| `iothub clear-node-buffers` | nodes drop readings stored while offline |
| `iothub reset-all [--yes]` | fresh start: buffers, readings, summaries, events, alerts (backs up first) |
| `iothub password` | change the dashboard admin password |
| `iothub test-alert` | send a test notification |
| `iothub restart` | restart the hub service |

## 3. Node settings

`iot_node/secrets.h`, copied from `secrets.example.h` and ignored by git. Rebuild and update the
nodes after changing it.

| Setting | Default | |
| --- | --- | --- |
| `WIFI_SSID`, `WIFI_PASS` | – | your 2.4 GHz Wi-Fi |
| `MQTT_USER`, `MQTT_PASS` | – | the Mosquitto login |
| `AP_PASS` | empty | password of the Pi's `IoTHub` Wi-Fi (Ethernet mode); empty = not used |
| `MQTT_HOST` | empty | fixed hub IP, tried after the gateway and mDNS |
| `MQTT_PORT` | `1883` | broker port |
| `LED_PIN` | `48` | onboard RGB LED (38 on DevKitC-1 v1.0) |
| `USE_DS18B20` | off | use a DS18B20 instead of the AM2305B |
| `SENSOR_PIN` | `1` | sensor data pin |

## 4. Hub settings

### 4.1 Config files

In `~/.config/iothub/` on the Pi, written by the setup scripts. Apply changes with `iothub restart`.

| File | Written by | Contents |
| --- | --- | --- |
| `mqtt.env` | `setup-ota.sh` | MQTT login |
| `dashboard.env` | `setup-dashboard.sh` | admin password, port |
| `alerts.env` | `setup-alerts.sh` | ntfy / Telegram, temperature and humidity limits |
| `adafruit.env` | `setup-adafruit.sh` | Adafruit IO username, key, `AIO_INCLUDE_PI` |
| `email.env` | `setup-email.sh` | sender, recipients, daily report time |
| `storage.env` | you | storage options below |

### 4.2 Storage options

| Option | Default | |
| --- | --- | --- |
| `FLUSH_MINUTES` | `1` | how often readings are written to the SD card |
| `RAW_KEEP_DAYS` | `30` | how long raw readings are kept (daily summaries are kept forever) |
| `FILL_MAX_MINUTES` | `15` | longest gap drawn as an estimate on charts |

## 5. Setup scripts

All in `pi/`, run on the Pi as your normal user. Each is safe to run again to change its settings.

| Script | |
| --- | --- |
| `setup-ota.sh` | MQTT login, firmware server on port 8000, `ota-push` |
| `setup-dashboard.sh` | hub service, dashboard on port 8080, `iothub` command |
| `setup-alerts.sh` | ntfy / Telegram notifications and limits |
| `setup-email.sh` | email reports via a Gmail app password |
| `setup-adafruit.sh` | Adafruit IO upload |
| `setup-network.sh` | Ethernet mode access point `IoTHub` |
| `protect-sd.sh` | logs in RAM, `noatime`, no SD swap, time zone |
| `optimize-boot.sh` | trims boot-time services and hardware probing |

## 6. MQTT

### 6.1 Topics

| Topic | Direction | |
| --- | --- | --- |
| `home/<node>/temp`, `hum` | node → hub | readings |
| `home/<node>/backlog` | node → hub | stored readings `{"r": [[epoch, temp, hum], …]}` (negative time = seconds ago) |
| `home/<node>/status` | node → hub | `online` / `offline` (retained, last will) |
| `home/<node>/info` | node → hub | JSON: firmware, IP, signal, uptime, sensor, buffered, boot count, reset reason (retained) |
| `home/<node>/ota/status`, `log` | node → hub | update progress, messages |
| `home/<node>/cmd` | hub → node | one node, see below |
| `home/all/cmd` | hub → nodes | every node |
| `home/hub/time` | hub → nodes | hub clock (epoch) for nodes without NTP |

### 6.2 Node commands

Send to `home/<node>/cmd` or `home/all/cmd`. Never publish commands as retained.

| Command | |
| --- | --- |
| `reboot` | restart |
| `info` | publish details now |
| `identify` | rainbow LED for 10 s |
| `brightness <0-255>` | LED brightness |
| `interval <seconds>` | report interval |
| `name <new-id>` | rename (restarts) |
| `clearbuffer` | drop stored offline readings |
| `ota <url>` | update from the hub's firmware server only |

## 7. HTTP API

Served by the hub on port 8080. Reads are open to guests; `POST` endpoints need an admin session
from `/api/login`.

### 7.1 Read

| Endpoint | |
| --- | --- |
| `GET /api/nodes` | nodes, open alerts, integrations, storage |
| `GET /api/history?hours=24&metric=temp` | chart series; `start` / `end` (epoch) also accepted; `metric` is `temp` or `hum` |
| `GET /api/daily?days=30&metric=hum` | daily min/avg/max |
| `GET /api/events` | event log; filters `category`, `level`, `node` (comma-separated), `q` (text), `since` (epoch), `limit`, `offset` |
| `GET /api/interruptions?days=30` | outages, gaps with causes, node restarts, completeness |
| `GET /api/health` | background jobs, storage, broker |
| `GET /api/config` | limits and settings (no secrets) |
| `GET /api/me` | current role |

### 7.2 Downloads

| Endpoint | |
| --- | --- |
| `GET /api/export.csv` | readings for a range; `fill=1` adds estimated values marked in a `source` column |
| `GET /api/daily.csv` | daily summaries |
| `GET /api/events.csv` | event log with the same filters |
| `GET /api/interruptions.csv` | outages, node restarts and gaps |

### 7.3 Actions

| Endpoint | Body | |
| --- | --- | --- |
| `POST /api/login` | `{"password": "…"}` | start an admin session |
| `POST /api/logout` | – | end it |
| `POST /api/nodes/<id>/cmd` | `{"cmd": "identify"}` | `reboot`, `info`, `identify`, `brightness N`, `interval S` |
| `POST /api/nodes/<id>/forget` | – | remove a node from the list and clear its retained topics |
| `POST /api/alerts/<key>/ack` | – | acknowledge / un-acknowledge an alert |
| `POST /api/alerts/test` | – | send a test notification |
| `POST /api/report/send` | `{"to": "a@b.c", "hours": 24}` | email a report |
