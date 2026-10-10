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
   4. [Google Home](#74-google-home)
8. [Predictions](#8-predictions)
   1. [Rain](#81-rain)
   2. [Temperature, dew and SD card](#82-temperature-dew-and-sd-card)
9. [App notifications](#9-app-notifications)
10. [How notifications are filtered](#10-how-notifications-are-filtered)
11. [Settings page](#11-settings-page)

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
| `iothub name <node> <Name> [indoor\|outdoor]` | friendly name and place, e.g. `iothub name node1 Balcony outdoor`; the ID and history stay the same; `-` as the name removes it |
| `iothub rename <node> <name>` | change the node's ID itself (it restarts; old readings stay under the old ID) |
| `iothub ota <node\|all> <file.bin>` | push a firmware image |

### 2.3 Reports and data

| Command | |
| --- | --- |
| `iothub summary [yesterday\|YYYY-MM-DD]` | the day in plain words: critical problems first, highs and lows with times, outages, restarts, warnings |
| `iothub weather` | outside now, the next two days, and current tips |
| `iothub rain` | chance of rain in the next 2 h at the outdoor node, the reasons, the predictor's score, questions waiting |
| `iothub rain yes\|no\|unsure` | answer the latest "did it rain?" question |
| `iothub outlook` | tonight's low and tomorrow's high per node, dew, how accurate past calls were, SD card outlook |
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
| `google.env` | `setup-google.sh` | Google Home link credentials, project ID, `GH_LED`, `GH_STATS`, `GH_INCLUDE_PI` |
| `google-service-account.json` | you | optional Home Graph key: live updates and automatic new-node sync |
| `weather.env` | `setup-weather.sh` | `WEATHER_LAT`, `WEATHER_LON`, `WEATHER_PLACE`, `RAIN_ALERTS`, `TIP_ALERTS`, `SUMMARY_TIME` (evening summary to the phone, default `21:00`) |
| `speaker.env` | `setup-speaker.sh` | `SPEAKER_NAME`, `SUMMARY_TIME` (daily spoken briefing), `SPEAK_ENGINE` (`piper` or `gtts`), `PIPER_VOICE` (default `en_GB-jenny_dioco-medium`), `SPEAK_TLD`, `SUMMARY_PUSH` |
| `nodes.env` | dashboard (node card) or `iothub name` | one line per node: `node1=Balcony\|outdoor`. Read live, no restart needed |
| `vapid.pem` | the hub, automatically | private key that signs app notifications. Keep it: a new key means every device has to turn notifications on again |
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
| `setup-weather.sh` | weather location, rain alerts, tips, summary time |
| `setup-speaker.sh` | optional spoken summary on a Google speaker (needs `SPEAKER_ENABLED=yes`) |
| `setup-google.sh` | Google Home link (prints the values for the Google Home Developer Console) |
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
| `GET /api/summary?day=yesterday` | day summary: `headline`, `text` (short spoken briefing), `attention` (problems, anomalies, warnings), `nodes` (highs/lows with times), `marks` (for charts), `sections` and `detail_text` |
| `GET /api/weather?hours=48` | outside now, hourly history and forecast, daily forecast, live insights |
| `GET /api/outlook` | per node: `tonight` (low, time, forecast), `tomorrow` (high/low), `dew`, `series` (next 24 h: time, expected, forecast); `score` (average error of past calls vs the baseline); `disk` (GB a month, days until nearly full) |
| `GET /api/inbox?limit=50&before=<ts>` | every notification, newest first: `title`, `body`, `level`, `cat`, `pushed`, `why` (reason it was kept quiet), `read`; plus `unread` and the last 24 h counts |
| `GET /api/settings` | the Settings page values, number of app devices, ntfy channels |
| `GET /api/push` | app notifications: `enabled`, public `key`, devices; `?endpoint=` adds this device's choices as `this` |
| `GET /api/rain` | rain `outlook` (chance in the next 2 h, reasons), `open` questions, your `recent` answers, `model` (examples, weights, "it's raining" signature), `score` (rains caught and false alarms, next to the forecast) |
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
| `POST /api/nodes/<id>/profile` | `{"name": "Balcony", "place": "outdoor"}` | friendly name and place; empty name = back to the ID |
| `POST /api/rain/<id>/answer` | `{"answer": "yes"}` | `yes`, `no` or `unsure`. Also accepts `?a=yes&sig=…` without a session: the signed links in the phone notification's buttons |
| `POST /api/rain/retrain` | – | retrain the rain predictor now (it does this every 6 h anyway) |
| `POST /api/inbox/read` | – | mark the inbox read |
| `POST /api/settings` | `{"quiet_start": "22:30"}` | change one or more settings (see section 11) |
| `POST /api/push/subscribe` | `{"subscription": {…}, "prefs": {"problems": true, "rain": true, "summary": true, "tips": false, "quiet": true}, "label": "Android app"}` | add or update a device (the browser's `PushSubscription.toJSON()`) |
| `POST /api/push/unsubscribe` | `{"endpoint": "…"}` | remove a device (no sign-in needed: knowing the endpoint is enough) |
| `POST /api/push/test` | `{"endpoint": "…"}` | send a test to that device |
| `POST /api/alerts/<key>/ack` | – | acknowledge / un-acknowledge an alert |
| `POST /api/alerts/test` | – | send a test notification |
| `POST /api/summary/speak` | `{"day": "yesterday"}` or `{"text": "…"}` | speak on the Google speaker |
| `POST /api/report/send` | `{"to": "a@b.c", "hours": 24}` | email a report |

### 7.4 Google Home

Used by Google, not by people. Enabled by `setup-google.sh`.

| Endpoint | |
| --- | --- |
| `GET/POST /google/authorize` | account linking page; approved with the admin password |
| `POST /google/token` | OAuth 2.0 token endpoint (authorization code and refresh) |
| `POST /google/fulfillment` | SYNC (device list), QUERY (readings), EXECUTE (actions below), DISCONNECT |

What Google Home sees. A node uses its friendly name if you gave it one ("Balcony"), otherwise the
spoken form of its ID (`node1` → "node 1"); "node 1" stays a nickname either way. After renaming,
say "sync my devices" (automatic if live updates are set up).

| Device | Type | Voice examples |
| --- | --- | --- |
| `node 1` | sensor: temperature, humidity | "what's the temperature of node 1", "what's the humidity of node 1" |
| `node 1 light` | light: the status LED | "turn off node 1 light", "set node 1 light to 20%" |
| `Find node 1`, `Restart node 1` | scenes | "activate find node 1", "activate restart node 1" |
| `node 1 high`, `node 1 low` | sensors: today's highest / lowest | "what's the temperature of node 1 high" |
| `hub` | sensor: Pi CPU temperature | "what's the temperature of the hub" |
| `Day summary` | scene: spoken briefing on the speaker, text to the phone | "activate day summary" |
| `Send hub report` | scene (only if email is set up) | "activate send hub report" |
| `Find all nodes` | scene | "activate find all nodes" |

Google only accepts commands that suit a device's type, so readings, the LED and actions are separate devices.
For shorter phrases ("find node 1", "turn off node 1"), add routines in the Google Home app with a
"When I say…" starter.

Options in `google.env`: `GH_LED`, `GH_STATS`, `GH_INCLUDE_PI` (all `yes` by default). After changing them, run
`iothub restart` and say "sync my devices".

**Reminders and summaries:** set `SUMMARY_TIME` with `setup-speaker.sh` for a daily spoken briefing, or create a
routine with any starter and "activate day summary" as the action.

A node whose sensor isn't reading answers as needing repair; an offline node as offline.

## 8. Predictions

### 8.1 Rain

Works when at least one node is marked **outdoor** (node card on the dashboard, or
`iothub name node1 Balcony outdoor`). Weather (`setup-weather.sh`) makes it much better but isn't required.

**What it looks at.** Every 15 minutes, from the outdoor node's graph: humidity level, how much it rose in the
last hour and three hours, how much the temperature changed in the last hour, and how close the air is to
saturation (temperature minus dew point). From the online weather: city temperature and humidity (the sensor
compared with them), rain chance and rain in the next two hours, pressure change over three hours, cloud cover.

**How it learns.** A small logistic regression, retrained every 6 hours on the last 60 days, sampled every 30 min.
Each moment is labelled "rain started within two hours" or "stayed dry" from what happened afterwards:
rain that both the forecast and the sensor saw, or that you confirmed, counts as rain; your "no, it stayed dry"
answers count as dry. Your answers weigh three times as much. With little data it stays close to a built-in
starting guess and moves away as examples come in.

**When it asks.** After a rain episode where the forecast and the sensor disagreed, or where it predicted rain
(60%+) and nothing else confirmed it, and for every episode until you've answered six. Questions go to the phone
(ntfy buttons answer them directly, at most 3 a day, between 7 AM and 10 PM) and wait on the dashboard for two
days. Episodes where both agree are recorded as rain without asking.

**"It's raining now".** Humidity up 10% in an hour to at least 85%, by default. Once you've answered at least three
yes and three no, the thresholds move to sit between the two (6–25% rise, 80–97% peak).

**Outdoor wording.** For outdoor nodes the hub skips indoor tips (open a window, mould) and humidity or daytime
temperature jumps aren't flagged as unusual, since sun, shade and rain cause them. Instead it reports rain now or
coming, good drying weather, when the sun is on the sensor (5°+ above the air temperature), and how the node
compares with the online temperature out of the sun.

Files: `~/iothub-data/rain_model.json` (current weights). Tables: `rain_checks` (episodes and your answers),
`rain_preds` (every prediction, kept 120 days, used for the score).

### 8.2 Temperature, dew and SD card

**Outdoor nodes:** the online hourly forecast, corrected by how this spot usually differs from it. The hub
learns the median difference for each hour of the day, separately for sunny and cloudy hours, from the last
14 days, so a balcony that gets the afternoon sun is expected to run hot exactly then. From that: tonight's
low, tomorrow's high, and a 24-hour curve on the Outlook card (solid = expected here, dashed = forecast).
Needs about a day of readings with weather before it starts.

**Dew:** if this spot's expected humidity overnight reaches 94% (air within about 1° of its dew point), the
Outlook card says when, and after 6 PM it's a tip ("bring in cushions") on the phone.

**Indoor nodes:** a straight-line fit of the room's daily high and low against the outdoor ones over the last
three weeks (at least 5 days). It also tells you how strongly the room follows the weather.

**Scoring:** each evening after 6 PM, tomorrow's calls are saved (`temp_preds`) and compared with what really
happened. Outdoor calls are compared with the plain forecast, indoor ones with "same as the day before".

**SD card:** the used space is recorded daily (`disk_hist`). After a week, the trend gives GB a month and when
the card would reach the "nearly full" limit; with old readings pruned it usually reads "steady".

## 9. App notifications

The installed dashboard app (or the browser) can get notifications straight from the hub with standard
Web Push, alongside or instead of ntfy/Telegram. Turn them on per device with **Phone notifications**
(admin sign-in) on the Activity page or in Settings. Each device chooses:

| Choice | Default | What |
| --- | --- | --- |
| Problems | on | everything that would page you: offline, power, sensor, limits. Critical alerts always come through |
| Rain | on | raining now, rain coming, and "did it rain?" questions with **Yes / No buttons** that answer directly |
| Evening summary | on | the day summary at `SUMMARY_TIME` |
| Tips | off | dew tonight, damp air, heat |
| Quiet at night | on | 10 PM to 7 AM non-critical notifications arrive silently |

Requirements: an https address (the Tailscale Funnel link, also what Share shows) and `python3-cryptography`
on the Pi (installed by `setup-dashboard.sh`). Android, desktop Chrome/Edge/Firefox work in the browser or the
installed app; on iPhone (iOS 16.4+) add it to the Home Screen first and turn notifications on from the app.
Devices that uninstall the app or block notifications are removed automatically.

## 10. How notifications are filtered

Everything goes into the inbox (the Activity page, `GET /api/inbox`, kept 60 days). What reaches a phone:

| Rule | |
| --- | --- |
| Offline delay | "Offline" is pushed only once a node has been gone for `offline_push_min` (default 5 min). If it comes back sooner, both messages stay in the inbox as "fixed itself quickly". Sensor faults wait 2 min more |
| Cool-down | the same thing isn't pushed again within 30 min (problems), 3 h (rain), 12 h (tips), 18 h (summary), checked in the database so a hub restart doesn't reset it |
| Flapping | a problem that starts 3 times in an hour is pushed once as "…, again and again", then kept quiet for two hours |
| Back to normal | pushed only if the problem itself was pushed (`recovery`: `pushed`, `always`, `never`) |
| Hourly limit | at most `max_per_hour` (default 4) non-critical pushes an hour |
| Quiet hours | `quiet_start` to `quiet_end` (default 22:00–07:00): non-critical notifications arrive silently |
| Hub messages | service restarts, settings changes and similar are only listed on the Activity page |
| ntfy / Telegram | `ntfy_mode`: `auto` (everything until a device has app notifications, then critical only), `all`, `critical`, `off` |
| Critical | power cuts, nodes offline, sensor faults and storage failures always get through the hourly limit and quiet hours |

## 11. Settings page

Changed on the dashboard (admin) and kept in the database; they win over the values in the `.env` files.

| Setting | Default (or `.env` key) |
| --- | --- |
| `ntfy_mode` | `auto` (`NTFY_MODE`) |
| `quiet_start`, `quiet_end` | `22:00`, `07:00` (`QUIET_START`, `QUIET_END`) |
| `max_per_hour` | `4` (`MAX_PUSH_PER_HOUR`) |
| `offline_push_min` | `5` (`OFFLINE_PUSH_MIN`) |
| `recovery` | `pushed` (`RECOVERY_PUSH`) |
| `summary_time`, `summary_push` | `21:00`, on (`SUMMARY_TIME`, `SUMMARY_PUSH`) |
| `rain_alerts`, `rain_questions`, `tips` | on (`RAIN_ALERTS`, `RAIN_QUESTIONS`, `TIP_ALERTS`) |
| `temp_high`, `temp_low`, `hum_high`, `hum_low` | none (`TEMP_HIGH`, … from `setup-alerts.sh`) |
